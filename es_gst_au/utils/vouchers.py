# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt
"""
Voucher construction and reconciliation.

A card payment is not a credit purchase. The document that represents it
is a single Journal Entry that moves money out of the bank and into an
expense account, splitting out GST where the treatment calls for it.
That is one document per bank line instead of an invoice, a payment and
a matching step.

Purchase and Sales Invoices remain available for genuine trade creditors
and real sales, selected per rule.
"""

from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt

from es_gst_au.utils.gst import (
	TREATMENT_GST,
	TREATMENT_NO_GST,
	TREATMENT_PRIVATE,
	get_gst_rate,
	money,
	split_inclusive,
)
from es_gst_au.utils.rules import (
	ACTION_IGNORE,
	ACTION_JOURNAL,
	ACTION_PURCHASE_INVOICE,
	ACTION_SALES_INVOICE,
)


def get_settings(company):
	name = frappe.db.get_value("ES Bank Import Settings", {"company": company})
	if not name:
		frappe.throw(
			_("No ES Bank Import Settings found for {0}. Create one before importing.").format(
				frappe.bold(company)
			)
		)
	return frappe.get_cached_doc("ES Bank Import Settings", name)


def get_gst_settings(company):
	name = frappe.db.get_value("ES GST Settings", {"company": company})
	if not name:
		frappe.throw(
			_("No ES GST Settings found for {0}. Create one before importing.").format(
				frappe.bold(company)
			)
		)
	return frappe.get_cached_doc("ES GST Settings", name)


def resolve_cost_center(company, preferred=None):
	"""
	Return a postable cost centre.

	ERPNext seeds a group cost centre named after the company, which
	cannot be posted to. Silently falling back to the child avoids a
	class of failure that only surfaces at submit time.
	"""
	if preferred and not cint(frappe.db.get_value("Cost Center", preferred, "is_group")):
		return preferred

	settings_cc = frappe.db.get_value("ES Bank Import Settings", {"company": company}, "cost_center")
	if settings_cc and not cint(frappe.db.get_value("Cost Center", settings_cc, "is_group")):
		return settings_cc

	fallback = frappe.db.get_value(
		"Cost Center", {"company": company, "is_group": 0}, "name", order_by="lft asc"
	)
	if not fallback:
		frappe.throw(_("No postable cost centre exists for {0}.").format(frappe.bold(company)))
	return fallback


def build_journal_entry(txn, decision, settings, gst_settings):
	"""
	Build (but do not insert) a Journal Entry for a bank transaction.

	Withdrawal of 100 at 10% GST:
	    Dr Expense   90.91
	    Dr GST        9.09
	    Cr Bank     100.00

	Deposit of 110 at 10% GST:
	    Dr Bank     110.00
	    Cr Income   100.00
	    Cr GST       10.00
	"""
	company = txn.company
	bank_account_gl = frappe.db.get_value("Bank Account", txn.bank_account, "account")
	if not bank_account_gl:
		frappe.throw(
			_("Bank Account {0} has no linked GL account.").format(frappe.bold(txn.bank_account))
		)

	cost_center = resolve_cost_center(company, decision.cost_center)
	is_debit = flt(txn.withdrawal) > 0
	gross = money(txn.withdrawal if is_debit else txn.deposit)

	treatment = decision.gst_treatment or TREATMENT_GST
	# Private and out-of-scope amounts carry no GST component at all.
	rate = get_gst_rate(company, treatment)
	net, gst = split_inclusive(gross, rate, treatment)

	gst_account = (
		gst_settings.gst_on_purchases_account if is_debit else gst_settings.gst_on_sales_account
	)
	if gst and not gst_account:
		frappe.throw(
			_("ES GST Settings for {0} has no GST account configured for {1}.").format(
				frappe.bold(company), _("purchases") if is_debit else _("sales")
			)
		)

	accounts = []

	if is_debit:
		accounts.append({
			"account": decision.account,
			"debit_in_account_currency": flt(net),
			"cost_center": cost_center,
			"user_remark": txn.description,
		})
		if gst:
			accounts.append({
				"account": gst_account,
				"debit_in_account_currency": flt(gst),
				"cost_center": cost_center,
			})
		accounts.append({
			"account": bank_account_gl,
			"credit_in_account_currency": flt(gross),
			"cost_center": cost_center,
		})
	else:
		accounts.append({
			"account": bank_account_gl,
			"debit_in_account_currency": flt(gross),
			"cost_center": cost_center,
		})
		accounts.append({
			"account": decision.account,
			"credit_in_account_currency": flt(net),
			"cost_center": cost_center,
			"user_remark": txn.description,
		})
		if gst:
			accounts.append({
				"account": gst_account,
				"credit_in_account_currency": flt(gst),
				"cost_center": cost_center,
			})

	# Attach the party to the expense/income leg when the rule names one,
	# so the ledger still shows who was paid without raising an invoice.
	if decision.party_type and decision.party:
		leg = accounts[0] if is_debit else accounts[1]
		leg["party_type"] = decision.party_type
		leg["party"] = decision.party

	je = frappe.new_doc("Journal Entry")
	je.update({
		"voucher_type": "Bank Entry",
		"company": company,
		"posting_date": txn.date,
		"user_remark": txn.description,
		"cheque_no": txn.reference_number or txn.name,
		"cheque_date": txn.date,
		"es_bank_transaction": txn.name,
		"es_gst_treatment": treatment,
		"es_is_capital": cint(decision.is_capital),
	})
	for row in accounts:
		je.append("accounts", row)

	return je


def build_purchase_invoice(txn, decision, settings, gst_settings):
	"""Build a Purchase Invoice for a genuine trade creditor."""
	if not decision.party:
		frappe.throw(_("Rule for {0} selects Purchase Invoice but names no supplier.").format(txn.name))

	cost_center = resolve_cost_center(txn.company, decision.cost_center)
	gross = money(txn.withdrawal)

	pi = frappe.new_doc("Purchase Invoice")
	pi.update({
		"company": txn.company,
		"supplier": decision.party,
		"posting_date": txn.date,
		"set_posting_time": 1,
		"bill_no": (txn.description or "")[:140],
		"bill_date": txn.date,
		"currency": txn.currency or frappe.db.get_value("Company", txn.company, "default_currency"),
		"conversion_rate": 1,
		"cost_center": cost_center,
		"update_stock": 0,
		"es_bank_transaction": txn.name,
		"es_gst_treatment": decision.gst_treatment,
	})
	pi.append("items", {
		"item_code": settings.default_expense_item,
		"description": (txn.description or "")[:140],
		"qty": 1,
		"uom": "Nos",
		"stock_uom": "Nos",
		"conversion_factor": 1,
		"rate": flt(gross),
		"expense_account": decision.account,
		"cost_center": cost_center,
	})
	if decision.gst_treatment == TREATMENT_GST and gst_settings.purchase_tax_template:
		pi.taxes_and_charges = gst_settings.purchase_tax_template
		_apply_tax_template(pi, gst_settings.purchase_tax_template, "Purchase Taxes and Charges")
	return pi


def build_sales_invoice(txn, decision, settings, gst_settings):
	"""Build a Sales Invoice for a genuine sale."""
	if not decision.party:
		frappe.throw(_("Rule for {0} selects Sales Invoice but names no customer.").format(txn.name))

	cost_center = resolve_cost_center(txn.company, decision.cost_center)
	gross = money(txn.deposit)

	si = frappe.new_doc("Sales Invoice")
	si.update({
		"company": txn.company,
		"customer": decision.party,
		"posting_date": txn.date,
		"set_posting_time": 1,
		"currency": txn.currency or frappe.db.get_value("Company", txn.company, "default_currency"),
		"conversion_rate": 1,
		"cost_center": cost_center,
		"update_stock": 0,
		"po_no": (txn.description or "")[:140],
		"es_bank_transaction": txn.name,
		"es_gst_treatment": decision.gst_treatment,
	})
	si.append("items", {
		"item_code": settings.default_income_item,
		"description": (txn.description or "")[:140],
		"qty": 1,
		"uom": "Nos",
		"stock_uom": "Nos",
		"conversion_factor": 1,
		"rate": flt(gross),
		"income_account": decision.account,
		"cost_center": cost_center,
	})
	if decision.gst_treatment == TREATMENT_GST and gst_settings.sales_tax_template:
		si.taxes_and_charges = gst_settings.sales_tax_template
		_apply_tax_template(si, gst_settings.sales_tax_template, "Sales Taxes and Charges")
	return si


def _apply_tax_template(doc, template, child_doctype):
	"""
	Copy tax rows from a template onto a document.

	Data-layer document creation does not fire the client-side fetch that
	normally populates the taxes table, so the rows are copied explicitly.
	"""
	rows = frappe.get_all(
		child_doctype,
		filters={"parent": template},
		fields=[
			"charge_type", "account_head", "rate", "description",
			"included_in_print_rate", "category", "add_deduct_tax", "cost_center",
		],
		order_by="idx asc",
	)
	doc.set("taxes", [])
	for row in rows:
		doc.append("taxes", {k: v for k, v in row.items() if v is not None})


def reconcile(txn, voucher_doctype, voucher_name, amount):
	"""
	Link a submitted voucher to its bank transaction and mark it reconciled.

	This is the step that actually clears the line in the Bank
	Reconciliation Tool. Creating the voucher alone leaves the bank
	transaction showing as unreconciled.
	"""
	txn = frappe.get_doc("Bank Transaction", txn.name if hasattr(txn, "name") else txn)

	already = [
		p for p in txn.payment_entries
		if p.payment_document == voucher_doctype and p.payment_entry == voucher_name
	]
	if already:
		return txn

	txn.append("payment_entries", {
		"payment_document": voucher_doctype,
		"payment_entry": voucher_name,
		"allocated_amount": flt(amount),
	})
	txn.save(ignore_permissions=True)
	txn.update_allocations()
	return txn


def process_transaction(txn, settings=None, gst_settings=None, submit=True):
	"""
	Code and post a single Bank Transaction end to end.

	Returns (voucher_doctype, voucher_name) or (None, None) when the line
	was ignored or could not be matched.
	"""
	from es_gst_au.utils.rules import classify

	if isinstance(txn, str):
		txn = frappe.get_doc("Bank Transaction", txn)

	settings = settings or get_settings(txn.company)
	gst_settings = gst_settings or get_gst_settings(txn.company)

	decision = classify(txn.company, txn.description, txn.withdrawal, txn.deposit)

	if not decision.matched or decision.action == ACTION_IGNORE:
		return None, None

	if not decision.account and decision.action == ACTION_JOURNAL:
		frappe.throw(
			_("Rule {0} matched but names no account.").format(frappe.bold(decision.rule))
		)

	builders = {
		ACTION_JOURNAL: build_journal_entry,
		ACTION_PURCHASE_INVOICE: build_purchase_invoice,
		ACTION_SALES_INVOICE: build_sales_invoice,
	}
	builder = builders.get(decision.action)
	if not builder:
		frappe.throw(_("Unsupported rule action {0}").format(decision.action))

	doc = builder(txn, decision, settings, gst_settings)
	doc.insert(ignore_permissions=True)

	if submit:
		doc.submit()
		amount = flt(txn.withdrawal) or flt(txn.deposit)
		reconcile(txn, doc.doctype, doc.name, amount)

	return doc.doctype, doc.name

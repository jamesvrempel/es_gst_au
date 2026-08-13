# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt
"""
BAS computation.

Labels follow the ATO BAS instructions. The GST section is built from
"taxable supply events" rather than from GL account balances, because
the same GST account can be hit by transactions with different BAS
treatments and a balance alone cannot tell them apart.

Accruals basis
    The event date is the voucher's posting date. An invoice is reported
    in the period it was issued regardless of when it was paid.

Cash basis
    The event date is the date money moved. Invoices are apportioned
    across the payments allocated against them, pro rata, so a part-paid
    invoice reports only the paid proportion. Journal Entries posted
    directly from a bank statement are already cash events and are taken
    at their posting date.

The apportionment matters: reporting a part-paid invoice in full on a
cash basis overstates the liability, and reporting it not at all
understates it.
"""

from collections import defaultdict
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, get_first_day, get_last_day, getdate

from es_gst_au.utils.gst import money

BASIS_CASH = "Cash"
BASIS_ACCRUALS = "Accruals"

SALES = "Sales"
PURCHASES = "Purchases"

# Quarterly periods for the Australian financial year.
QUARTERS = {
	1: ((7, 1), (9, 30)),    # Q1 Jul-Sep, due 28 Oct
	2: ((10, 1), (12, 31)),  # Q2 Oct-Dec, due 28 Feb
	3: ((1, 1), (3, 31)),    # Q3 Jan-Mar, due 28 Apr
	4: ((4, 1), (6, 30)),    # Q4 Apr-Jun, due 28 Jul
}

QUARTER_DUE = {1: (10, 28), 2: (2, 28), 3: (4, 28), 4: (7, 28)}


def get_period_dates(cycle, year, period):
	"""
	Resolve a reporting period to (from_date, to_date, due_date).

	`year` is the calendar year the period starts in for monthly, and the
	financial year ending June for quarterly.
	"""
	if cycle == "Monthly":
		start = getdate(f"{year}-{int(period):02d}-01")
		end = get_last_day(start)
		# Monthly BAS is due on the 21st of the following month.
		due_month = start.month + 1
		due_year = year + 1 if due_month > 12 else year
		due_month = 1 if due_month > 12 else due_month
		due = getdate(f"{due_year}-{due_month:02d}-21")
		return start, end, due

	if cycle == "Quarterly":
		(sm, sd), (em, ed) = QUARTERS[int(period)]
		# Q1 and Q2 fall in the calendar year before the FY ends.
		start_year = year - 1 if sm >= 7 else year
		end_year = year - 1 if em >= 7 else year
		start = getdate(f"{start_year}-{sm:02d}-{sd:02d}")
		end = getdate(f"{end_year}-{em:02d}-{ed:02d}")
		dm, dd = QUARTER_DUE[int(period)]
		due_year = end.year + 1 if dm < end.month else end.year
		due = getdate(f"{due_year}-{dm:02d}-{dd:02d}")
		return start, end, due

	# Annual
	start = getdate(f"{year - 1}-07-01")
	end = getdate(f"{year}-06-30")
	return start, end, getdate(f"{year}-10-31")


def _treatment_map():
	"""treatment -> (sales_label, purchase_label, gst_applicable, claimable)"""
	rows = frappe.get_all(
		"ES GST Treatment",
		fields=["name", "bas_sales_label", "bas_purchase_label", "gst_applicable", "claimable"],
	)
	return {
		r.name: {
			"sales_label": r.bas_sales_label or "",
			"purchase_label": r.bas_purchase_label or "",
			"gst_applicable": cint(r.gst_applicable),
			"claimable": cint(r.claimable),
		}
		for r in rows
	}


def _invoice_events(company, from_date, to_date, basis, doctype, party_field):
	"""
	Return taxable events from invoices.

	On accruals these are the invoices themselves. On cash they are the
	payments allocated against invoices, carrying the invoice's treatment
	and a pro-rata share of its net and GST.
	"""
	direction = SALES if doctype == "Sales Invoice" else PURCHASES
	events = []

	if basis == BASIS_ACCRUALS:
		invoices = frappe.get_all(
			doctype,
			filters={
				"company": company, "docstatus": 1,
				"posting_date": ["between", [from_date, to_date]],
			},
			fields=[
				"name", "posting_date", party_field + " as party", "net_total",
				"total_taxes_and_charges", "grand_total", "es_gst_treatment",
			],
		)
		for inv in invoices:
			events.append({
				"date": inv.posting_date,
				"voucher_type": doctype,
				"voucher": inv.name,
				"party": inv.party,
				"treatment": inv.es_gst_treatment,
				"direction": direction,
				"gross": money(inv.grand_total),
				"net": money(inv.net_total),
				"gst": money(inv.total_taxes_and_charges),
				"is_capital": 0,
			})
		return events

	# Cash basis: follow the payments.
	party_type = "Customer" if doctype == "Sales Invoice" else "Supplier"
	allocations = frappe.db.sql(
		"""
		SELECT per.reference_name AS invoice, pe.posting_date, pe.name AS payment,
		       per.allocated_amount
		FROM `tabPayment Entry Reference` per
		INNER JOIN `tabPayment Entry` pe ON pe.name = per.parent
		WHERE pe.docstatus = 1 AND pe.company = %(company)s
		  AND per.reference_doctype = %(doctype)s
		  AND pe.posting_date BETWEEN %(from_date)s AND %(to_date)s
		""",
		{"company": company, "doctype": doctype, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)
	if not allocations:
		return events

	invoice_names = list({a.invoice for a in allocations})
	invoices = {
		i.name: i
		for i in frappe.get_all(
			doctype,
			filters={"name": ["in", invoice_names]},
			fields=[
				"name", "posting_date", party_field + " as party", "net_total",
				"total_taxes_and_charges", "grand_total", "es_gst_treatment",
			],
		)
	}

	for alloc in allocations:
		inv = invoices.get(alloc.invoice)
		if not inv or not flt(inv.grand_total):
			continue
		# Pro-rata share of the invoice represented by this payment.
		share = Decimal(str(flt(alloc.allocated_amount))) / Decimal(str(flt(inv.grand_total)))
		events.append({
			"date": alloc.posting_date,
			"voucher_type": "Payment Entry",
			"voucher": alloc.payment,
			"against": inv.name,
			"party": inv.party,
			"treatment": inv.es_gst_treatment,
			"direction": direction,
			"gross": money(flt(alloc.allocated_amount)),
			"net": money(Decimal(str(flt(inv.net_total))) * share),
			"gst": money(Decimal(str(flt(inv.total_taxes_and_charges))) * share),
			"is_capital": 0,
		})

	return events


def _journal_events(company, from_date, to_date, gst_settings):
	"""
	Return taxable events from Journal Entries.

	These are cash events by construction (they are posted from bank
	transactions), so they appear on both bases at their posting date.
	The net and GST are read off the JE's own lines rather than
	recomputed, so a manual adjustment to a JE is respected.
	"""
	journals = frappe.get_all(
		"Journal Entry",
		filters={
			"company": company, "docstatus": 1,
			"posting_date": ["between", [from_date, to_date]],
			"es_gst_treatment": ["is", "set"],
		},
		fields=["name", "posting_date", "es_gst_treatment", "es_is_capital", "user_remark"],
	)
	if not journals:
		return []

	names = [j.name for j in journals]
	lines = frappe.get_all(
		"Journal Entry Account",
		filters={"parent": ["in", names]},
		fields=["parent", "account", "debit", "credit", "party_type", "party"],
	)

	gst_accounts = {
		gst_settings.get("gst_on_purchases_account"),
		gst_settings.get("gst_on_sales_account"),
	}
	gst_accounts.discard(None)

	bank_accounts = set(
		frappe.get_all(
			"Account",
			filters={"company": company, "account_type": "Bank", "is_group": 0},
			pluck="name",
		)
	)

	by_parent = defaultdict(list)
	for line in lines:
		by_parent[line.parent].append(line)

	events = []
	for je in journals:
		rows = by_parent.get(je.name, [])
		gst = Decimal("0")
		net = Decimal("0")
		party = None
		direction = None

		for row in rows:
			if row.account in gst_accounts:
				gst += Decimal(str(flt(row.debit) - flt(row.credit)))
				continue
			if row.account in bank_accounts:
				# Credit to bank means money out, i.e. a purchase.
				direction = PURCHASES if flt(row.credit) else SALES
				continue
			# The remaining leg is the expense or income line.
			net += Decimal(str(flt(row.debit) - flt(row.credit)))
			party = party or row.party

		if direction is None:
			continue

		gst = abs(gst)
		net = abs(net)
		events.append({
			"date": je.posting_date,
			"voucher_type": "Journal Entry",
			"voucher": je.name,
			"party": party,
			"treatment": je.es_gst_treatment,
			"direction": direction,
			"gross": money(net + gst),
			"net": money(net),
			"gst": money(gst),
			"is_capital": cint(je.es_is_capital),
			"remark": je.user_remark,
		})

	return events


def collect_events(company, from_date, to_date, basis):
	"""Gather every taxable supply event in the period."""
	gst_settings = (
		frappe.db.get_value(
			"ES GST Settings", {"company": company},
			["gst_on_purchases_account", "gst_on_sales_account"], as_dict=True,
		)
		or {}
	)

	events = []
	events += _journal_events(company, from_date, to_date, gst_settings)
	events += _invoice_events(company, from_date, to_date, basis, "Sales Invoice", "customer")
	events += _invoice_events(company, from_date, to_date, basis, "Purchase Invoice", "supplier")
	return events


def compute(company, from_date, to_date, basis=BASIS_CASH):
	"""
	Compute BAS label values for a period.

	Returns a dict of labels to Decimal amounts, plus the underlying
	events so the worksheet can drill through.
	"""
	events = collect_events(company, from_date, to_date, basis)
	treatments = _treatment_map()

	labels = defaultdict(lambda: Decimal("0.00"))

	for event in events:
		meta = treatments.get(event["treatment"]) or {}
		gross = money(event["gross"])
		gst = money(event["gst"])

		if event["direction"] == SALES:
			# G1 is every sale, GST inclusive.
			labels["G1"] += gross
			label = meta.get("sales_label")
			if label and label != "G1":
				labels[label] += gross
			if meta.get("gst_applicable"):
				labels["_gst_on_sales"] += gst
		else:
			# G10 capital, G11 non-capital. Both are GST inclusive.
			if event["is_capital"]:
				labels["G10"] += gross
			else:
				labels["G11"] += gross
			label = meta.get("purchase_label")
			if label and label not in ("G10", "G11"):
				labels[label] += gross
			if meta.get("gst_applicable") and meta.get("claimable"):
				labels["_gst_on_purchases"] += gst

	# Derived sales labels
	labels["G5"] = labels["G2"] + labels["G3"] + labels["G4"]
	labels["G6"] = labels["G1"] - labels["G5"]
	labels["G8"] = labels["G6"] + labels["G7"]
	# G9 is the ATO's formula (G8 divided by eleven). We report the sum of
	# actual GST charged instead when the two differ, because rounding on
	# individual invoices is what was really collected.
	labels["G9"] = labels["_gst_on_sales"] or money(labels["G8"] / 11)

	# Derived purchase labels
	labels["G12"] = labels["G10"] + labels["G11"]
	labels["G16"] = labels["G13"] + labels["G14"] + labels["G15"]
	labels["G17"] = labels["G12"] - labels["G16"]
	labels["G19"] = labels["G17"] + labels["G18"]
	labels["G20"] = labels["_gst_on_purchases"] or money(labels["G19"] / 11)

	# Summary
	labels["1A"] = labels["G9"]
	labels["1B"] = labels["G20"]

	return {
		"labels": {k: money(v) for k, v in labels.items() if not k.startswith("_")},
		"events": events,
		"basis": basis,
		"from_date": from_date,
		"to_date": to_date,
	}


def compute_payg(company, from_date, to_date, gst_settings):
	"""
	W1/W2 PAYG withholding from payroll, where HRMS is present.

	Returns zeros rather than guessing when payroll data is unavailable,
	because a wrong W2 is a withholding misstatement.
	"""
	result = {"W1": Decimal("0.00"), "W2": Decimal("0.00")}

	if not cint(gst_settings.get("report_paygw")):
		return result

	if not frappe.db.exists("DocType", "Salary Slip"):
		return result

	slips = frappe.db.sql(
		"""
		SELECT SUM(gross_pay) AS gross, SUM(total_deduction) AS deductions
		FROM `tabSalary Slip`
		WHERE docstatus = 1 AND company = %(company)s
		  AND start_date >= %(from_date)s AND end_date <= %(to_date)s
		""",
		{"company": company, "from_date": from_date, "to_date": to_date},
		as_dict=True,
	)
	if slips and slips[0].gross:
		result["W1"] = money(slips[0].gross)

	paygw_account = gst_settings.get("paygw_account")
	if paygw_account:
		withheld = frappe.db.sql(
			"""
			SELECT SUM(credit - debit) AS amount
			FROM `tabGL Entry`
			WHERE company = %(company)s AND account = %(account)s
			  AND is_cancelled = 0 AND posting_date BETWEEN %(from_date)s AND %(to_date)s
			""",
			{
				"company": company, "account": paygw_account,
				"from_date": from_date, "to_date": to_date,
			},
			as_dict=True,
		)
		if withheld and withheld[0].amount:
			result["W2"] = money(withheld[0].amount)

	return result


@frappe.whitelist()
def preview(company, from_date, to_date, basis=BASIS_CASH):
	"""Compute without creating a return, for the worksheet report."""
	frappe.has_permission("ES BAS Return", throw=True)
	result = compute(company, getdate(from_date), getdate(to_date), basis)
	return {"labels": {k: flt(v) for k, v in result["labels"].items()}}

# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, getdate

from es_gst_au.utils.bas import (
	BASIS_ACCRUALS,
	BASIS_CASH,
	compute,
	compute_payg,
	get_period_dates,
)
from es_gst_au.utils.gst import money

# (label, description, section, derived, editable)
# Derived labels are calculated from others and never entered directly.
# Editable labels are the adjustment and PAYG fields the ATO expects a
# preparer to complete manually.
LABEL_DEFINITIONS = [
	("G1", "Total sales (including any GST)", "Sales", 0, 0),
	("G2", "Export sales", "Sales", 0, 0),
	("G3", "Other GST-free sales", "Sales", 0, 0),
	("G4", "Input taxed sales", "Sales", 0, 0),
	("G5", "G2 + G3 + G4", "Sales", 1, 0),
	("G6", "Total sales subject to GST (G1 minus G5)", "Sales", 1, 0),
	("G7", "Adjustments (if applicable)", "Sales", 0, 1),
	("G8", "Total sales subject to GST after adjustments (G6 + G7)", "Sales", 1, 0),
	("G9", "GST on sales (G8 divided by eleven)", "Sales", 1, 0),
	("G10", "Capital purchases (including any GST)", "Purchases", 0, 0),
	("G11", "Non-capital purchases (including any GST)", "Purchases", 0, 0),
	("G12", "G10 + G11", "Purchases", 1, 0),
	("G13", "Purchases for making input taxed sales", "Purchases", 0, 0),
	("G14", "Purchases without GST in the price", "Purchases", 0, 0),
	("G15", "Estimated purchases for private use or not income tax deductible", "Purchases", 0, 0),
	("G16", "G13 + G14 + G15", "Purchases", 1, 0),
	("G17", "Total purchases subject to GST (G12 minus G16)", "Purchases", 1, 0),
	("G18", "Adjustments (if applicable)", "Purchases", 0, 1),
	("G19", "Total purchases subject to GST after adjustments (G17 + G18)", "Purchases", 1, 0),
	("G20", "GST on purchases (G19 divided by eleven)", "Purchases", 1, 0),
	("1A", "GST on sales", "Summary", 1, 0),
	("1B", "GST on purchases", "Summary", 1, 0),
	("W1", "Total salary, wages and other payments", "PAYG", 0, 1),
	("W2", "Amount withheld from payments shown at W1", "PAYG", 0, 1),
	("W3", "Other amounts withheld", "PAYG", 0, 1),
	("W4", "Amount withheld where no ABN is quoted", "PAYG", 0, 1),
	("W5", "W2 + W3 + W4", "PAYG", 1, 0),
]

DERIVED = {label for label, _d, _s, derived, _e in LABEL_DEFINITIONS if derived}
EDITABLE = {label for label, _d, _s, _dv, editable in LABEL_DEFINITIONS if editable}


class ESBASReturn(Document):
	def validate(self):
		self.set_period()
		self.check_overlap()
		if not self.labels:
			self.calculate()
		else:
			self.recalculate_derived()
		self.set_summary()

	def before_submit(self):
		if not self.labels:
			frappe.throw(_("Calculate the return before submitting."))

	def on_submit(self):
		frappe.msgprint(
			_("BAS period {0} to {1} is now locked. Postings dated within it will be blocked.").format(
				frappe.bold(self.from_date), frappe.bold(self.to_date)
			)
		)

	def on_cancel(self):
		if cint(self.lodged):
			frappe.throw(
				_("This return is marked as lodged with the ATO. Unmark it before cancelling.")
			)

	# ------------------------------------------------------------------

	def set_period(self):
		settings = frappe.db.get_value(
			"ES GST Settings", {"company": self.company},
			["abn", "reporting_cycle", "accounting_basis", "gst_registered"], as_dict=True,
		)
		if not settings:
			frappe.throw(
				_("No ES GST Settings found for {0}.").format(frappe.bold(self.company))
			)
		if not cint(settings.gst_registered):
			frappe.msgprint(
				_("{0} is not marked as registered for GST. Check ES GST Settings before lodging.").format(
					frappe.bold(self.company)
				),
				indicator="orange", title=_("Not registered"),
			)

		self.abn = settings.abn
		self.reporting_cycle = self.reporting_cycle or settings.reporting_cycle
		self.accounting_basis = self.accounting_basis or settings.accounting_basis

		if self.reporting_cycle == "Quarterly" and not (1 <= cint(self.period) <= 4):
			frappe.throw(_("Quarterly period must be 1 to 4."))
		if self.reporting_cycle == "Monthly" and not (1 <= cint(self.period) <= 12):
			frappe.throw(_("Monthly period must be 1 to 12."))

		self.from_date, self.to_date, self.due_date = get_period_dates(
			self.reporting_cycle, cint(self.period_year), cint(self.period)
		)

	def check_overlap(self):
		existing = frappe.db.sql(
			"""
			SELECT name FROM `tabES BAS Return`
			WHERE company = %(company)s AND docstatus < 2 AND name != %(name)s
			  AND from_date <= %(to_date)s AND to_date >= %(from_date)s
			""",
			{
				"company": self.company, "name": self.name or "new",
				"from_date": self.from_date, "to_date": self.to_date,
			},
		)
		if existing:
			frappe.throw(
				_("BAS return {0} already covers part of this period.").format(
					frappe.bold(existing[0][0])
				)
			)

	@frappe.whitelist()
	def calculate(self):
		"""Recompute every non-editable label from the ledger."""
		result = compute(
			self.company, getdate(self.from_date), getdate(self.to_date), self.accounting_basis
		)
		computed = result["labels"]

		gst_settings = frappe.db.get_value(
			"ES GST Settings", {"company": self.company},
			["report_paygw", "paygw_account"], as_dict=True,
		) or {}
		payg = compute_payg(
			self.company, getdate(self.from_date), getdate(self.to_date), gst_settings
		)

		# Preserve anything the preparer typed into an editable label.
		retained = {
			row.label: flt(row.amount) for row in (self.labels or []) if row.label in EDITABLE
		}

		self.set("labels", [])
		for label, description, section, derived, editable in LABEL_DEFINITIONS:
			if label in retained and editable:
				amount = retained[label]
			elif label in payg:
				amount = flt(payg[label])
			else:
				amount = flt(computed.get(label, 0))

			self.append("labels", {
				"label": label,
				"label_description": description,
				"section": section,
				"amount": amount,
				"is_derived": derived,
				"is_editable": editable,
			})

		self.recalculate_derived()
		self.set_summary()
		return {"labels": len(self.labels), "events": len(result["events"])}

	def recalculate_derived(self):
		"""Recompute derived labels so manual adjustments flow through."""
		value = {row.label: flt(row.amount) for row in self.labels}

		value["G5"] = value.get("G2", 0) + value.get("G3", 0) + value.get("G4", 0)
		value["G6"] = value.get("G1", 0) - value["G5"]
		value["G8"] = value["G6"] + value.get("G7", 0)
		value["G12"] = value.get("G10", 0) + value.get("G11", 0)
		value["G16"] = value.get("G13", 0) + value.get("G14", 0) + value.get("G15", 0)
		value["G17"] = value["G12"] - value["G16"]
		value["G19"] = value["G17"] + value.get("G18", 0)
		value["W5"] = value.get("W2", 0) + value.get("W3", 0) + value.get("W4", 0)

		# 1A and 1B follow G9/G20, which calculate() set from actual GST
		# charged rather than the divide-by-eleven approximation. Only
		# fall back to the formula when an adjustment has moved G8/G19.
		if value.get("G7"):
			value["G9"] = round(value["G8"] / 11, 2)
		if value.get("G18"):
			value["G20"] = round(value["G19"] / 11, 2)

		value["1A"] = value.get("G9", 0)
		value["1B"] = value.get("G20", 0)

		for row in self.labels:
			if row.label in DERIVED:
				row.amount = flt(value.get(row.label, 0))

	def set_summary(self):
		value = {row.label: flt(row.amount) for row in self.labels}
		self.gst_on_sales = value.get("1A", 0)
		self.gst_on_purchases = value.get("1B", 0)
		self.payg_withholding = value.get("W5", 0)

		# 8A = 1A + PAYG withholding + PAYG instalment; 8B = 1B
		owed = flt(self.gst_on_sales) + flt(self.payg_withholding) + flt(self.payg_instalment)
		credits = flt(self.gst_on_purchases)
		self.net_amount = flt(owed - credits, 2)
		self.net_label = (
			_("Amount you owe the ATO") if self.net_amount >= 0
			else _("Refund from the ATO")
		)


def get_locked_period(company, posting_date):
	"""Return a submitted BAS return covering this date, if any."""
	if not company or not posting_date:
		return None
	return frappe.db.get_value(
		"ES BAS Return",
		{
			"company": company, "docstatus": 1,
			"from_date": ["<=", posting_date], "to_date": [">=", posting_date],
		},
		"name",
	)


def validate_period_not_locked(doc, method=None):
	"""
	Block postings into a lodged BAS period.

	Attached to Journal Entry, Purchase Invoice, Sales Invoice and
	Payment Entry. Once a period is reported to the ATO, changing the
	underlying numbers silently would put the ledger out of step with
	what was lodged.
	"""
	if doc.doctype == "ES BAS Return":
		return
	if not getattr(doc, "company", None) or not getattr(doc, "posting_date", None):
		return

	locked = get_locked_period(doc.company, doc.posting_date)
	if not locked:
		return

	if frappe.flags.get("es_gst_allow_locked_period"):
		return

	frappe.throw(
		_("{0} is dated {1}, which falls inside BAS return {2}. "
		  "Cancel or amend that return before posting into the period.").format(
			frappe.bold(doc.doctype), frappe.bold(doc.posting_date), frappe.bold(locked)
		),
		title=_("BAS period locked"),
	)


@frappe.whitelist()
def create_for_period(company, cycle, year, period, basis=None):
	"""Convenience entry point used by the workspace shortcut."""
	frappe.has_permission("ES BAS Return", ptype="create", throw=True)
	doc = frappe.new_doc("ES BAS Return")
	doc.company = company
	doc.reporting_cycle = cycle
	doc.period_year = cint(year)
	doc.period = cint(period)
	if basis:
		doc.accounting_basis = basis
	doc.insert()
	doc.calculate()
	doc.save()
	return doc.name

# Copyright (c) 2026, Enterprise Systems Australia and contributors
"""
BAS Worksheet.

Shows every taxable supply event behind a BAS label so a figure can be
traced to the voucher that produced it. This is what you need when the
ATO asks how a number was arrived at.
"""

import frappe
from frappe import _
from frappe.utils import flt, getdate

from es_gst_au.utils.bas import BASIS_CASH, collect_events, compute


def execute(filters=None):
	filters = frappe._dict(filters or {})

	if not filters.company:
		frappe.throw(_("Select a company."))
	if not (filters.from_date and filters.to_date):
		frappe.throw(_("Select a date range."))

	basis = filters.basis or BASIS_CASH
	events = collect_events(
		filters.company, getdate(filters.from_date), getdate(filters.to_date), basis
	)

	treatments = {
		t.name: t
		for t in frappe.get_all(
			"ES GST Treatment",
			fields=["name", "bas_sales_label", "bas_purchase_label", "gst_applicable", "claimable"],
		)
	}

	rows = []
	for event in sorted(events, key=lambda e: (e["date"], e.get("voucher") or "")):
		meta = treatments.get(event["treatment"])
		if event["direction"] == "Sales":
			label = "G1"
			secondary = meta.bas_sales_label if meta else ""
		else:
			label = "G10" if event.get("is_capital") else "G11"
			secondary = meta.bas_purchase_label if meta else ""

		if filters.get("label") and filters.label not in (label, secondary):
			continue

		rows.append({
			"date": event["date"],
			"voucher_type": event["voucher_type"],
			"voucher": event["voucher"],
			"against": event.get("against"),
			"party": event.get("party"),
			"description": (event.get("remark") or "")[:120],
			"treatment": event["treatment"],
			"bas_label": label,
			"secondary_label": secondary if secondary not in (label, "") else "",
			"net": flt(event["net"]),
			"gst": flt(event["gst"]),
			"gross": flt(event["gross"]),
		})

	return get_columns(), rows, None, None, get_summary(filters, basis)


def get_columns():
	return [
		{"label": _("Date"), "fieldname": "date", "fieldtype": "Date", "width": 95},
		{"label": _("Type"), "fieldname": "voucher_type", "fieldtype": "Data", "width": 120},
		{"label": _("Voucher"), "fieldname": "voucher", "fieldtype": "Dynamic Link",
		 "options": "voucher_type", "width": 170},
		{"label": _("Against"), "fieldname": "against", "fieldtype": "Data", "width": 150},
		{"label": _("Party"), "fieldname": "party", "fieldtype": "Data", "width": 170},
		{"label": _("Description"), "fieldname": "description", "fieldtype": "Data", "width": 260},
		{"label": _("Treatment"), "fieldname": "treatment", "fieldtype": "Link",
		 "options": "ES GST Treatment", "width": 110},
		{"label": _("Label"), "fieldname": "bas_label", "fieldtype": "Data", "width": 70},
		{"label": _("Also"), "fieldname": "secondary_label", "fieldtype": "Data", "width": 70},
		{"label": _("Net"), "fieldname": "net", "fieldtype": "Currency", "width": 110},
		{"label": _("GST"), "fieldname": "gst", "fieldtype": "Currency", "width": 100},
		{"label": _("Gross"), "fieldname": "gross", "fieldtype": "Currency", "width": 110},
	]


def get_summary(filters, basis):
	result = compute(
		filters.company, getdate(filters.from_date), getdate(filters.to_date), basis
	)
	labels = result["labels"]
	net = flt(labels.get("1A", 0)) - flt(labels.get("1B", 0))

	return [
		{"label": _("G1 Total sales"), "value": flt(labels.get("G1", 0)),
		 "datatype": "Currency", "indicator": "Blue"},
		{"label": _("1A GST on sales"), "value": flt(labels.get("1A", 0)),
		 "datatype": "Currency", "indicator": "Orange"},
		{"label": _("G11 Non-capital purchases"), "value": flt(labels.get("G11", 0)),
		 "datatype": "Currency", "indicator": "Blue"},
		{"label": _("G10 Capital purchases"), "value": flt(labels.get("G10", 0)),
		 "datatype": "Currency", "indicator": "Blue"},
		{"label": _("1B GST on purchases"), "value": flt(labels.get("1B", 0)),
		 "datatype": "Currency", "indicator": "Green"},
		{"label": _("Net (owed) / refund"), "value": net, "datatype": "Currency",
		 "indicator": "Red" if net >= 0 else "Green"},
	]

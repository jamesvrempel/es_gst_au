# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from es_gst_au.utils.gst import (
	TREATMENT_EXPORT,
	TREATMENT_GST,
	TREATMENT_GST_FREE,
	TREATMENT_INPUT_TAXED,
	TREATMENT_NO_GST,
	TREATMENT_PRIVATE,
)

CUSTOM_FIELDS = {
	"Bank Transaction": [
		{
			"fieldname": "es_row_hash",
			"label": "Statement Row Hash",
			"fieldtype": "Data",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"unique": 1,
			"insert_after": "reference_number",
			"description": "Fingerprint used to prevent the same statement line importing twice.",
		},
		{
			"fieldname": "es_import_batch",
			"label": "Import Batch",
			"fieldtype": "Link",
			"options": "ES Bank Import Batch",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "es_row_hash",
		},
	],
	"Journal Entry": [
		{
			"fieldname": "es_bank_transaction",
			"label": "Source Bank Transaction",
			"fieldtype": "Link",
			"options": "Bank Transaction",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "cheque_date",
		},
		{
			"fieldname": "es_gst_treatment",
			"label": "GST Treatment",
			"fieldtype": "Link",
			"options": "ES GST Treatment",
			"insert_after": "es_bank_transaction",
		},
		{
			"fieldname": "es_is_capital",
			"label": "Capital Acquisition",
			"fieldtype": "Check",
			"insert_after": "es_gst_treatment",
			"description": "Reported at G10 rather than G11.",
		},
	],
	"Purchase Invoice": [
		{
			"fieldname": "es_bank_transaction",
			"label": "Source Bank Transaction",
			"fieldtype": "Link",
			"options": "Bank Transaction",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "bill_date",
		},
		{
			"fieldname": "es_gst_treatment",
			"label": "GST Treatment",
			"fieldtype": "Link",
			"options": "ES GST Treatment",
			"insert_after": "es_bank_transaction",
		},
	],
	"Sales Invoice": [
		{
			"fieldname": "es_bank_transaction",
			"label": "Source Bank Transaction",
			"fieldtype": "Link",
			"options": "Bank Transaction",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "po_date",
		},
		{
			"fieldname": "es_gst_treatment",
			"label": "GST Treatment",
			"fieldtype": "Link",
			"options": "ES GST Treatment",
			"insert_after": "es_bank_transaction",
		},
	],
}

# Shipped defaults. BAS labels follow the ATO BAS instructions:
#   G1  total sales (GST inclusive)
#   G2  export sales
#   G3  other GST-free sales
#   G10 capital purchases
#   G11 non-capital purchases
#   G13 purchases for making input taxed sales
#   G15 purchases for private use / not income tax deductible
DEFAULT_TREATMENTS = [
	{
		"treatment_name": TREATMENT_GST,
		"description": "Taxable supply at the standard rate.",
		"gst_applicable": 1,
		"bas_sales_label": "G1",
		"bas_purchase_label": "G11",
		"claimable": 1,
	},
	{
		"treatment_name": TREATMENT_GST_FREE,
		"description": "GST-free supply, such as basic food, most health and education.",
		"gst_applicable": 0,
		"bas_sales_label": "G3",
		"bas_purchase_label": "G14",
		"claimable": 0,
	},
	{
		"treatment_name": TREATMENT_INPUT_TAXED,
		"description": "Input taxed, such as residential rent and financial supplies. No credit claimable.",
		"gst_applicable": 0,
		"bas_sales_label": "G4",
		"bas_purchase_label": "G13",
		"claimable": 0,
	},
	{
		"treatment_name": TREATMENT_EXPORT,
		"description": "GST-free export sale.",
		"gst_applicable": 0,
		"bas_sales_label": "G2",
		"bas_purchase_label": "",
		"claimable": 0,
	},
	{
		"treatment_name": TREATMENT_NO_GST,
		"description": "Outside the GST system: wages, ATO payments, internal transfers, dividends.",
		"gst_applicable": 0,
		"bas_sales_label": "",
		"bas_purchase_label": "",
		"claimable": 0,
	},
	{
		"treatment_name": TREATMENT_PRIVATE,
		"description": "Private or non-deductible. No input tax credit may be claimed.",
		"gst_applicable": 0,
		"bas_sales_label": "",
		"bas_purchase_label": "G15",
		"claimable": 0,
	},
]

# A conservative starter rule set. Deliberately small: these are patterns
# that are unambiguous across every Australian business. Merchant-level
# rules belong to the client, not the app, and are learned from the
# unmatched queue.
STARTER_RULES = [
	{
		"rule_name": "ATO Payment",
		"priority": 10,
		"match_type": "Contains",
		"pattern": "ATO",
		"direction": "Debit",
		"gst_treatment": TREATMENT_NO_GST,
		"action": "Journal Entry",
	},
	{
		"rule_name": "Bank Fee",
		"priority": 20,
		"match_type": "Regex",
		"pattern": r"(?i)\b(fee|charge|acct keeping|account keeping)\b",
		"direction": "Debit",
		"gst_treatment": TREATMENT_GST_FREE,
		"action": "Journal Entry",
	},
	{
		"rule_name": "Interest Received",
		"priority": 25,
		"match_type": "Contains",
		"pattern": "interest",
		"direction": "Credit",
		"gst_treatment": TREATMENT_INPUT_TAXED,
		"action": "Journal Entry",
	},
	{
		"rule_name": "Internal Transfer",
		"priority": 30,
		"match_type": "Regex",
		"pattern": r"(?i)(transfer to|transfer from|internal transfer)",
		"gst_treatment": TREATMENT_NO_GST,
		"action": "Ignore",
	},
	{
		"rule_name": "Wages and Salary",
		"priority": 40,
		"match_type": "Regex",
		"pattern": r"(?i)\b(wages?|salary|salaries|payroll)\b",
		"gst_treatment": TREATMENT_NO_GST,
		"action": "Journal Entry",
	},
	{
		"rule_name": "Superannuation",
		"priority": 45,
		"match_type": "Regex",
		"pattern": r"(?i)\b(super|superannuation|smsf)\b",
		"direction": "Debit",
		"gst_treatment": TREATMENT_NO_GST,
		"action": "Journal Entry",
	},
]


def after_install():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	seed_treatments()
	seed_starter_rules()
	frappe.db.commit()


def after_migrate():
	create_custom_fields(CUSTOM_FIELDS, ignore_validate=True)
	seed_treatments()
	frappe.db.commit()


def seed_treatments():
	for treatment in DEFAULT_TREATMENTS:
		if frappe.db.exists("ES GST Treatment", treatment["treatment_name"]):
			continue
		doc = frappe.new_doc("ES GST Treatment")
		doc.update(treatment)
		doc.insert(ignore_permissions=True)


def seed_starter_rules():
	"""
	Seed rules without accounts attached.

	The account is intentionally left blank: it differs per chart of
	accounts, and a rule pointing at the wrong account is worse than a
	rule that asks to be completed. Each is created disabled so nothing
	posts until an accountant has reviewed and coded it.
	"""
	for rule in STARTER_RULES:
		if frappe.db.exists("ES Bank Rule", rule["rule_name"]):
			continue
		doc = frappe.new_doc("ES Bank Rule")
		doc.update(rule)
		doc.enabled = 0
		doc.insert(ignore_permissions=True)

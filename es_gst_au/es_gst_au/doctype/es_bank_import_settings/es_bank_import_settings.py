# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint


class ESBankImportSettings(Document):
	def validate(self):
		self.validate_cost_center()
		self.validate_items()

	def validate_cost_center(self):
		"""
		ERPNext seeds a group cost centre named after the company, which
		cannot be posted to. Catching it here avoids a failure that would
		otherwise only surface when a voucher is submitted.
		"""
		if not self.cost_center:
			return

		details = frappe.db.get_value(
			"Cost Center", self.cost_center, ["is_group", "company"], as_dict=True
		)
		if not details:
			return

		if cint(details.is_group):
			suggestion = frappe.db.get_value(
				"Cost Center", {"company": self.company, "is_group": 0}, "name", order_by="lft asc"
			)
			frappe.throw(
				_("{0} is a group cost centre and cannot be posted to. Try {1}.").format(
					frappe.bold(self.cost_center), frappe.bold(suggestion or _("a child cost centre"))
				)
			)

		if details.company != self.company:
			frappe.throw(
				_("Cost Center {0} belongs to {1}, not {2}.").format(
					frappe.bold(self.cost_center), frappe.bold(details.company),
					frappe.bold(self.company),
				)
			)

	def validate_items(self):
		"""Invoice-creating rules need default items; journal rules do not."""
		uses_invoices = frappe.db.exists(
			"ES Bank Rule",
			{"action": ["in", ["Purchase Invoice", "Sales Invoice"]], "enabled": 1},
		)
		if not uses_invoices:
			return

		if not self.default_expense_item and not self.default_income_item:
			frappe.msgprint(
				_("Rules exist that create invoices, but no default items are set here."),
				indicator="orange", title=_("Incomplete setup"),
			)

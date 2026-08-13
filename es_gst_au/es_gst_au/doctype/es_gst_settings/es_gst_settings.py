# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class ESGSTSettings(Document):
	def validate(self):
		self.validate_abn()
		self.validate_accounts()
		self.validate_rate()

	def validate_abn(self):
		if not self.abn:
			return

		digits = re.sub(r"\s", "", self.abn)
		if not digits.isdigit() or len(digits) != 11:
			frappe.throw(_("ABN must be 11 digits."))

		# ATO modulus 89 checksum.
		weights = [10, 1, 3, 5, 7, 9, 11, 13, 15, 17, 19]
		total = sum(
			(int(d) - 1 if i == 0 else int(d)) * w
			for i, (d, w) in enumerate(zip(digits, weights))
		)
		if total % 89 != 0:
			frappe.throw(
				_("ABN {0} fails the ATO checksum. Check for a typo.").format(frappe.bold(self.abn))
			)

		self.abn = digits

	def validate_accounts(self):
		for field in ("gst_on_purchases_account", "gst_on_sales_account", "paygw_account"):
			account = self.get(field)
			if not account:
				continue
			details = frappe.db.get_value(
				"Account", account, ["is_group", "company"], as_dict=True
			)
			if not details:
				continue
			if cint(details.is_group):
				frappe.throw(
					_("{0} is a group account and cannot be posted to.").format(frappe.bold(account))
				)
			if details.company != self.company:
				frappe.throw(
					_("Account {0} belongs to {1}, not {2}.").format(
						frappe.bold(account), frappe.bold(details.company), frappe.bold(self.company)
					)
				)

		if cint(self.gst_registered) and not self.gst_on_purchases_account:
			frappe.msgprint(
				_("No GST on Purchases account is set. Input tax credits cannot be recorded."),
				indicator="orange", title=_("Incomplete GST setup"),
			)

	def validate_rate(self):
		if flt(self.gst_rate) < 0 or flt(self.gst_rate) > 100:
			frappe.throw(_("GST Rate must be between 0 and 100."))
		if cint(self.gst_registered) and not flt(self.gst_rate):
			frappe.throw(_("GST Rate is required when registered for GST."))

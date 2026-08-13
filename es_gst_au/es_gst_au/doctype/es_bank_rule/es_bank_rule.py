# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import re

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt


class ESBankRule(Document):
	def validate(self):
		self.validate_pattern()
		self.validate_amounts()
		self.validate_account()
		self.validate_party()
		self.clear_rule_cache()

	def on_trash(self):
		self.clear_rule_cache()

	def validate_pattern(self):
		if not (self.pattern or "").strip():
			frappe.throw(_("Pattern is required."))

		if self.match_type == "Regex":
			try:
				re.compile(self.pattern)
			except re.error as exc:
				frappe.throw(
					_("Pattern is not valid regular expression: {0}").format(exc),
					title=_("Invalid regex"),
				)

	def validate_amounts(self):
		if flt(self.min_amount) and flt(self.max_amount):
			if flt(self.min_amount) > flt(self.max_amount):
				frappe.throw(_("Min Amount cannot be greater than Max Amount."))

	def validate_account(self):
		if self.action == "Ignore":
			return

		if not self.account:
			# Allowed while drafting a rule set, but it cannot post.
			frappe.msgprint(
				_("This rule has no account and will not post until one is set."),
				indicator="orange", title=_("Incomplete rule"), alert=True,
			)
			return

		account = frappe.db.get_value(
			"Account", self.account, ["is_group", "company", "root_type"], as_dict=True
		)
		if not account:
			frappe.throw(_("Account {0} does not exist.").format(frappe.bold(self.account)))

		if cint(account.is_group):
			frappe.throw(
				_("{0} is a group account and cannot be posted to.").format(
					frappe.bold(self.account)
				)
			)

		if self.company and account.company != self.company:
			frappe.throw(
				_("Account {0} belongs to {1}, not {2}.").format(
					frappe.bold(self.account), frappe.bold(account.company),
					frappe.bold(self.company),
				)
			)

		# A debit rule pointing at an income account (or vice versa) is
		# almost always a mistake, and one that is tedious to unpick once
		# it has posted a few hundred vouchers.
		if self.direction == "Debit" and account.root_type == "Income":
			frappe.msgprint(
				_("This rule matches money out but codes to an income account. Check that this is intended."),
				indicator="orange", title=_("Check account"),
			)
		elif self.direction == "Credit" and account.root_type == "Expense":
			frappe.msgprint(
				_("This rule matches money in but codes to an expense account. Check that this is intended."),
				indicator="orange", title=_("Check account"),
			)

	def validate_party(self):
		if self.action == "Purchase Invoice" and self.party_type != "Supplier":
			frappe.throw(_("Purchase Invoice rules require a Party Type of Supplier."))
		if self.action == "Sales Invoice" and self.party_type != "Customer":
			frappe.throw(_("Sales Invoice rules require a Party Type of Customer."))
		if self.party and not self.party_type:
			frappe.throw(_("Set a Party Type before selecting a Party."))
		if self.party_type and self.party:
			if not frappe.db.exists(self.party_type, self.party):
				frappe.throw(
					_("{0} {1} does not exist.").format(self.party_type, frappe.bold(self.party))
				)

	def clear_rule_cache(self):
		"""Rules are request-cached, so a change must invalidate the cache."""
		frappe.cache().delete_keys("es_gst_au_rules")


@frappe.whitelist()
def test_pattern(pattern, match_type, sample):
	"""Test a pattern against a sample description from the rule form."""
	frappe.has_permission("ES Bank Rule", throw=True)

	squash = lambda text: re.sub(r"[^a-z0-9]", "", str(text or "").lower())

	if match_type == "Regex":
		try:
			return {"matched": bool(re.search(pattern, sample, re.IGNORECASE))}
		except re.error as exc:
			return {"matched": False, "error": str(exc)}

	needle, haystack = squash(pattern), squash(sample)
	if match_type == "Contains":
		matched = needle in haystack
	elif match_type == "Does Not Contain":
		matched = needle not in haystack
	elif match_type == "Starts With":
		matched = haystack.startswith(needle)
	elif match_type == "Exact":
		matched = haystack == needle
	else:
		matched = False

	return {"matched": matched}

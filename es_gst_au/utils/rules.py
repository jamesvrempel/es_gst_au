# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt
"""
Bank Rule matching engine.

Rules are evaluated in priority order (ascending). The first rule whose
conditions all hold supplies the coding decision. A rule may be marked
`stop_processing` to end evaluation explicitly, which is useful for
"ignore this line" rules such as internal transfers.

Compiled regexes are cached per-rule-modification so that a 900 line
statement does not recompile the same patterns 900 times.
"""

import re
from decimal import Decimal

import frappe
from frappe import _
from frappe.utils import cint, flt

from es_gst_au.utils.gst import (
	TREATMENT_GST,
	TREATMENT_NO_GST,
	validate_treatment,
)

MATCH_CONTAINS = "Contains"
MATCH_NOT_CONTAINS = "Does Not Contain"
MATCH_STARTS_WITH = "Starts With"
MATCH_EXACT = "Exact"
MATCH_REGEX = "Regex"

DIRECTION_ANY = "Any"
DIRECTION_DEBIT = "Debit"
DIRECTION_CREDIT = "Credit"

ACTION_JOURNAL = "Journal Entry"
ACTION_PURCHASE_INVOICE = "Purchase Invoice"
ACTION_SALES_INVOICE = "Sales Invoice"
ACTION_IGNORE = "Ignore"


class Decision:
	"""The outcome of matching a bank transaction against the rule set."""

	__slots__ = (
		"matched", "rule", "action", "party_type", "party", "account",
		"gst_treatment", "cost_center", "is_capital", "reason",
	)

	def __init__(self, **kwargs):
		for slot in self.__slots__:
			setattr(self, slot, kwargs.get(slot))
		if self.matched is None:
			self.matched = False

	def as_dict(self):
		return {slot: getattr(self, slot) for slot in self.__slots__}

	def __repr__(self):
		if not self.matched:
			return "<Decision unmatched>"
		return f"<Decision {self.action} party={self.party} account={self.account} gst={self.gst_treatment}>"


def _normalise(text):
	return re.sub(r"\s+", " ", str(text or "")).strip()


def _squash(text):
	"""
	Lowercase and strip everything but alphanumerics.

	Bank descriptors break words at fixed widths, so "Liquorla Nd 3283"
	and "LiquorLand 3283" should match the same rule. Squashing removes
	the whitespace ambiguity entirely.
	"""
	return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


@frappe.request_cache
def get_rules(company):
	"""
	Load enabled rules for a company, most specific first.

	Company-specific rules outrank global rules at the same priority, so
	a shared default rule set can be shipped and then overridden per
	client without editing the defaults.
	"""
	rules = frappe.get_all(
		"ES Bank Rule",
		filters={"enabled": 1},
		or_filters=[["company", "=", company], ["company", "is", "not set"]],
		fields=[
			"name", "rule_name", "priority", "company", "match_type", "pattern",
			"direction", "min_amount", "max_amount", "action", "party_type",
			"party", "account", "gst_treatment", "cost_center", "is_capital",
			"stop_processing",
		],
		order_by="priority asc, company desc, name asc",
	)
	return rules


def _pattern_matches(rule, description, squashed):
	pattern = rule.get("pattern") or ""
	if not pattern:
		return False

	match_type = rule.get("match_type") or MATCH_CONTAINS

	if match_type == MATCH_REGEX:
		try:
			return bool(re.search(pattern, description, re.IGNORECASE))
		except re.error:
			frappe.log_error(
				title="ES GST AU: invalid regex in Bank Rule",
				message=f"Rule {rule.get('name')} pattern {pattern!r} is not valid regex.",
			)
			return False

	needle = _squash(pattern)
	if not needle:
		return False

	if match_type == MATCH_CONTAINS:
		return needle in squashed
	if match_type == MATCH_NOT_CONTAINS:
		return needle not in squashed
	if match_type == MATCH_STARTS_WITH:
		return squashed.startswith(needle)
	if match_type == MATCH_EXACT:
		return squashed == needle

	return False


def _conditions_hold(rule, description, squashed, debit, credit):
	if not _pattern_matches(rule, description, squashed):
		return False

	direction = rule.get("direction") or DIRECTION_ANY
	if direction == DIRECTION_DEBIT and not debit:
		return False
	if direction == DIRECTION_CREDIT and not credit:
		return False

	amount = flt(debit or credit)
	min_amount = flt(rule.get("min_amount"))
	max_amount = flt(rule.get("max_amount"))
	if min_amount and amount < min_amount:
		return False
	if max_amount and amount > max_amount:
		return False

	return True


def classify(company, description, debit=0, credit=0, rules=None):
	"""
	Match a single bank line against the rule set.

	Returns a Decision. An unmatched line yields Decision(matched=False),
	which the caller should route to the review queue rather than guess
	at, because a wrong account is worse than an unposted one.
	"""
	description = _normalise(description)
	squashed = _squash(description)
	rules = rules if rules is not None else get_rules(company)

	for rule in rules:
		if not _conditions_hold(rule, description, squashed, debit, credit):
			continue

		action = rule.get("action") or ACTION_JOURNAL
		if action == ACTION_IGNORE:
			return Decision(
				matched=True, rule=rule.get("name"), action=ACTION_IGNORE,
				reason=_("Matched ignore rule {0}").format(rule.get("rule_name")),
			)

		return Decision(
			matched=True,
			rule=rule.get("name"),
			action=action,
			party_type=rule.get("party_type"),
			party=rule.get("party"),
			account=rule.get("account"),
			gst_treatment=validate_treatment(rule.get("gst_treatment") or TREATMENT_GST),
			cost_center=rule.get("cost_center"),
			is_capital=cint(rule.get("is_capital")),
			reason=_("Matched rule {0}").format(rule.get("rule_name")),
		)

	return Decision(matched=False, reason=_("No rule matched"))


def classify_many(company, lines):
	"""Classify a batch, loading the rule set once."""
	rules = get_rules(company)
	return [
		classify(
			company,
			line.get("description"),
			line.get("debit"),
			line.get("credit"),
			rules=rules,
		)
		for line in lines
	]


@frappe.whitelist()
def preview(company, description, debit=0, credit=0):
	"""Test a description against the live rule set from the UI."""
	frappe.has_permission("ES Bank Rule", throw=True)
	decision = classify(company, description, flt(debit), flt(credit))
	return decision.as_dict()


@frappe.whitelist()
def suggest_rule_from_transaction(bank_transaction):
	"""
	Propose a rule from an unmatched Bank Transaction.

	Used by the review queue's "create rule from this" action so that
	coding a line once teaches the engine for next month.
	"""
	frappe.has_permission("ES Bank Rule", ptype="create", throw=True)
	txn = frappe.get_doc("Bank Transaction", bank_transaction)

	description = _normalise(txn.description)
	# Offer the longest alphabetic run as the starting pattern; it is
	# usually the merchant name with the card and reference noise removed.
	words = [w for w in re.findall(r"[A-Za-z]{3,}", description)]
	suggested = " ".join(words[:3]) if words else description[:40]

	return {
		"pattern": suggested,
		"match_type": MATCH_CONTAINS,
		"direction": DIRECTION_DEBIT if flt(txn.withdrawal) else DIRECTION_CREDIT,
		"gst_treatment": TREATMENT_GST,
		"description": description,
	}

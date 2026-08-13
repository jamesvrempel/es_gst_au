# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt
"""
Australian GST calculation helpers.

The ATO rule for a GST-inclusive amount is that the GST component is
exactly one eleventh of the total, rounded to the nearest cent. Deriving
the net as (total - gst) rather than (total / 1.1) guarantees that the
components always re-sum to the original amount, which is what keeps a
voucher tying back to the bank line to the cent.
"""

from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _

# BAS treatment codes. These are stable identifiers used in Bank Rules and
# on GL entries; the human-readable labels live in the ES GST Treatment
# doctype so they can be relabelled without breaking stored data.
TREATMENT_GST = "GST"                      # Taxable, 10%
TREATMENT_GST_FREE = "GST Free"            # e.g. basic food, most health
TREATMENT_INPUT_TAXED = "Input Taxed"      # e.g. residential rent, financial supplies
TREATMENT_EXPORT = "Export"                # GST-free export sales
TREATMENT_NO_GST = "No GST"                # Out of scope: wages, transfers, ATO payments
TREATMENT_PRIVATE = "Private"              # Non-deductible / non-claimable

TAXABLE_TREATMENTS = (TREATMENT_GST,)

DEFAULT_GST_RATE = Decimal("10")


def _d(value):
	"""Coerce to Decimal without float artefacts."""
	if isinstance(value, Decimal):
		return value
	return Decimal(str(value or 0))


def money(value):
	"""Round to 2dp using half-up, which is what the ATO expects."""
	return _d(value).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def split_inclusive(total, rate=None, treatment=TREATMENT_GST):
	"""
	Split a GST-inclusive amount into (net, gst).

	Returns Decimals. net + gst == total is guaranteed.

	>>> split_inclusive(165.38)
	(Decimal('150.35'), Decimal('15.03'))
	>>> split_inclusive(100.00)
	(Decimal('90.91'), Decimal('9.09'))
	"""
	total = money(total)

	if treatment not in TAXABLE_TREATMENTS:
		return total, Decimal("0.00")

	rate = _d(rate if rate is not None else DEFAULT_GST_RATE)
	if rate <= 0:
		return total, Decimal("0.00")

	# GST = total * rate / (100 + rate). At 10% this is the familiar 1/11.
	gst = money(total * rate / (Decimal("100") + rate))
	net = total - gst
	return net, gst


def split_exclusive(net, rate=None, treatment=TREATMENT_GST):
	"""Split a GST-exclusive amount into (net, gst)."""
	net = money(net)

	if treatment not in TAXABLE_TREATMENTS:
		return net, Decimal("0.00")

	rate = _d(rate if rate is not None else DEFAULT_GST_RATE)
	if rate <= 0:
		return net, Decimal("0.00")

	return net, money(net * rate / Decimal("100"))


def get_gst_rate(company, treatment=TREATMENT_GST, on_date=None):
	"""
	Resolve the GST rate for a company on a given date.

	Kept as a lookup rather than a constant so that a future rate change
	does not require restating historical periods.
	"""
	if treatment not in TAXABLE_TREATMENTS:
		return Decimal("0")

	rate = frappe.db.get_value(
		"ES GST Settings", {"company": company}, "gst_rate"
	)
	return _d(rate) if rate else DEFAULT_GST_RATE


def validate_treatment(treatment):
	valid = {
		TREATMENT_GST,
		TREATMENT_GST_FREE,
		TREATMENT_INPUT_TAXED,
		TREATMENT_EXPORT,
		TREATMENT_NO_GST,
		TREATMENT_PRIVATE,
	}
	if treatment not in valid:
		frappe.throw(
			_("Unknown GST treatment {0}. Expected one of: {1}").format(
				frappe.bold(treatment), ", ".join(sorted(valid))
			)
		)
	return treatment

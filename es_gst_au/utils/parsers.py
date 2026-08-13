# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt
"""
Bank statement parsers.

Australian banks each export a different CSV shape and none of them
include a header row consistently. Rather than ask the user to map
columns every month, we sniff the format and normalise everything to a
common row structure:

    {date, description, debit, credit, balance, reference}

Amounts are returned as positive Decimals in separate debit/credit
fields, matching how ERPNext's Bank Transaction stores them.
"""

import csv
import hashlib
import io
import re
from datetime import datetime
from decimal import Decimal, InvalidOperation

import frappe
from frappe import _

from es_gst_au.utils.gst import money

# Date formats seen across AU bank exports, most specific first.
DATE_FORMATS = (
	"%d/%m/%Y", "%d-%m-%Y", "%d %b %Y", "%d %B %Y",
	"%Y-%m-%d", "%d/%m/%y", "%d-%b-%y", "%d-%b-%Y",
)


def parse_date(value):
	value = (value or "").strip()
	if not value:
		return None
	for fmt in DATE_FORMATS:
		try:
			return datetime.strptime(value, fmt).date()
		except ValueError:
			continue
	return None


def parse_amount(value):
	"""Parse an amount, tolerating $ signs, thousands separators and (brackets)."""
	if value is None:
		return Decimal("0")
	if isinstance(value, (int, float, Decimal)):
		return money(value)

	text = str(value).strip()
	if not text:
		return Decimal("0")

	negative = text.startswith("(") and text.endswith(")")
	text = text.strip("()").replace("$", "").replace(",", "").replace(" ", "")
	if not text or text in {"-", "."}:
		return Decimal("0")

	try:
		amount = money(text)
	except (InvalidOperation, ValueError):
		return Decimal("0")

	return -amount if negative else amount


def clean_description(text):
	"""Collapse whitespace and strip the noise banks pad descriptions with."""
	text = re.sub(r"\s+", " ", str(text or "")).strip()
	return text


def row_hash(bank_account, date, description, debit, credit, balance=None):
	"""
	Stable fingerprint for a statement line, used to prevent double-import.

	Balance is included when present because it is what distinguishes two
	genuinely separate transactions that happen to share a date, amount
	and merchant, which does occur (two identical coffees, same day).
	When the bank does not supply a running balance the caller must
	supply an occurrence index instead, otherwise legitimate duplicates
	would collapse into one.
	"""
	parts = [
		str(bank_account or ""),
		str(date or ""),
		clean_description(description).lower(),
		str(money(debit)),
		str(money(credit)),
		str(money(balance)) if balance is not None else "",
	]
	return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()


class StatementParser:
	"""Base parser. Subclasses declare how to recognise and read a format."""

	name = "Generic"
	# Column aliases, lowercased. First match wins.
	DATE_COLS = ("date", "transaction date", "processed date", "posting date")
	DESC_COLS = ("description", "narrative", "transaction details", "details", "particulars", "memo")
	DEBIT_COLS = ("debit", "withdrawal", "withdrawals", "debit amount", "money out")
	CREDIT_COLS = ("credit", "deposit", "deposits", "credit amount", "money in")
	AMOUNT_COLS = ("amount", "transaction amount", "value")
	BALANCE_COLS = ("balance", "running balance", "closing balance")
	REF_COLS = ("reference", "transaction id", "receipt number", "serial")

	@classmethod
	def matches(cls, headers, sample_rows):
		return False

	@classmethod
	def _find(cls, headers, candidates):
		normalised = {h.strip().lower(): i for i, h in enumerate(headers) if h}
		for candidate in candidates:
			if candidate in normalised:
				return normalised[candidate]
		# Fall back to a substring match for banks that append units, e.g.
		# "Debit Amount (AUD)".
		for key, index in normalised.items():
			for candidate in candidates:
				if candidate in key:
					return index
		return None

	@classmethod
	def parse(cls, headers, rows):
		i_date = cls._find(headers, cls.DATE_COLS)
		i_desc = cls._find(headers, cls.DESC_COLS)
		i_debit = cls._find(headers, cls.DEBIT_COLS)
		i_credit = cls._find(headers, cls.CREDIT_COLS)
		i_amount = cls._find(headers, cls.AMOUNT_COLS)
		i_balance = cls._find(headers, cls.BALANCE_COLS)
		i_ref = cls._find(headers, cls.REF_COLS)

		if i_date is None or i_desc is None:
			frappe.throw(
				_("Could not identify date and description columns in this file. "
				  "Expected one of {0} and one of {1}.").format(
					", ".join(cls.DATE_COLS), ", ".join(cls.DESC_COLS)
				)
			)

		if i_debit is None and i_credit is None and i_amount is None:
			frappe.throw(_("Could not identify an amount column in this file."))

		out = []
		for raw in rows:
			if not any((cell or "").strip() for cell in raw):
				continue

			def cell(index):
				return raw[index] if index is not None and index < len(raw) else ""

			date = parse_date(cell(i_date))
			if not date:
				# Almost always a subtotal, header repeat or footer line.
				continue

			debit = credit = Decimal("0")
			if i_amount is not None and i_debit is None and i_credit is None:
				# Single signed amount column: negative is money out.
				amount = parse_amount(cell(i_amount))
				if amount < 0:
					debit = -amount
				else:
					credit = amount
			else:
				debit = abs(parse_amount(cell(i_debit)))
				credit = abs(parse_amount(cell(i_credit)))

			if not debit and not credit:
				continue

			out.append({
				"date": date,
				"description": clean_description(cell(i_desc)),
				"debit": debit,
				"credit": credit,
				"balance": parse_amount(cell(i_balance)) if i_balance is not None else None,
				"reference": clean_description(cell(i_ref)) if i_ref is not None else "",
			})

		return out


class NABParser(StatementParser):
	name = "NAB"
	DEBIT_COLS = ("debit", "withdrawal", "debit amount")
	CREDIT_COLS = ("credit", "deposit", "credit amount")

	@classmethod
	def matches(cls, headers, sample_rows):
		joined = " ".join(h.lower() for h in headers if h)
		return "nab" in joined or (
			cls._find(headers, cls.DEBIT_COLS) is not None
			and cls._find(headers, cls.CREDIT_COLS) is not None
		)


class CBAParser(StatementParser):
	"""
	CommBank exports headerless CSVs: Date, Amount, Description, Balance.
	"""
	name = "CBA"

	@classmethod
	def matches(cls, headers, sample_rows):
		if headers and any(h.strip() for h in headers):
			return False
		if not sample_rows:
			return False
		first = sample_rows[0]
		return len(first) == 4 and parse_date(first[0]) is not None

	@classmethod
	def parse(cls, headers, rows):
		all_rows = ([headers] if headers else []) + list(rows)
		out = []
		for raw in all_rows:
			if len(raw) < 3:
				continue
			date = parse_date(raw[0])
			if not date:
				continue
			amount = parse_amount(raw[1])
			out.append({
				"date": date,
				"description": clean_description(raw[2]),
				"debit": -amount if amount < 0 else Decimal("0"),
				"credit": amount if amount > 0 else Decimal("0"),
				"balance": parse_amount(raw[3]) if len(raw) > 3 else None,
				"reference": "",
			})
		return out


class ANZParser(StatementParser):
	name = "ANZ"
	DESC_COLS = ("description", "transaction details", "details")

	@classmethod
	def matches(cls, headers, sample_rows):
		joined = " ".join(h.lower() for h in headers if h)
		return "anz" in joined


class WestpacParser(StatementParser):
	name = "Westpac"
	DESC_COLS = ("narrative", "description", "transaction details")
	DEBIT_COLS = ("debit amount", "debit", "withdrawal")
	CREDIT_COLS = ("credit amount", "credit", "deposit")

	@classmethod
	def matches(cls, headers, sample_rows):
		normalised = {h.strip().lower() for h in headers if h}
		return "narrative" in normalised


PARSERS = (CBAParser, WestpacParser, NABParser, ANZParser, StatementParser)


def sniff_and_parse(content, filename="", forced_format=None):
	"""
	Detect the bank format and return normalised rows.

	`content` may be bytes or str. Returns (parser_name, rows).
	"""
	if isinstance(content, bytes):
		for encoding in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
			try:
				content = content.decode(encoding)
				break
			except UnicodeDecodeError:
				continue
		else:
			frappe.throw(_("Could not decode the uploaded file as text."))

	# Strip fully blank leading lines that some exports carry.
	text = content.lstrip("\r\n")

	try:
		dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
	except csv.Error:
		dialect = csv.excel

	reader = csv.reader(io.StringIO(text), dialect)
	all_rows = [r for r in reader]
	if not all_rows:
		frappe.throw(_("The uploaded file is empty."))

	# A header row is one where the first cell is not parseable as a date.
	first = all_rows[0]
	has_header = parse_date(first[0] if first else "") is None
	headers = first if has_header else []
	body = all_rows[1:] if has_header else all_rows

	if forced_format:
		parser = next((p for p in PARSERS if p.name == forced_format), StatementParser)
	else:
		parser = next((p for p in PARSERS if p.matches(headers, body[:5])), StatementParser)

	rows = parser.parse(headers, body)
	if not rows:
		frappe.throw(
			_("No transactions could be read from this file using the {0} format.").format(parser.name)
		)

	return parser.name, rows

# Copyright (c) 2026, Enterprise Systems Australia and contributors
"""Scheduled maintenance tasks."""

import frappe
from frappe.utils import add_days, getdate, nowdate

from es_gst_au.utils.bas import get_period_dates


def retry_failed_postings(limit=200):
	"""
	Re-attempt rows that errored during posting.

	Most failures are transient (a lock, or a party created since). Rows
	that fail repeatedly stay visible on their batch for review.
	"""
	rows = frappe.get_all(
		"ES Bank Import Batch Row",
		filters={"status": "Error", "voucher": ["is", "not set"]},
		fields=["name", "parent", "bank_transaction"],
		limit=limit,
	)
	for row in rows:
		if not row.bank_transaction:
			continue
		try:
			from es_gst_au.utils.vouchers import process_transaction

			voucher_type, voucher = process_transaction(row.bank_transaction, submit=True)
			if voucher:
				frappe.db.set_value(
					"ES Bank Import Batch Row", row.name,
					{"voucher_type": voucher_type, "voucher": voucher,
					 "status": "Posted", "error": None},
					update_modified=False,
				)
				frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"ES GST AU: retry failed for {row.bank_transaction}")


def flag_due_bas_periods(days_ahead=14):
	"""
	Notify Accounts Managers when a BAS period has closed and is approaching
	its due date without a submitted return.
	"""
	today = getdate(nowdate())
	horizon = getdate(add_days(today, days_ahead))

	companies = frappe.get_all(
		"ES GST Settings",
		filters={"gst_registered": 1},
		fields=["company", "reporting_cycle"],
	)

	for row in companies:
		periods = range(1, 5) if row.reporting_cycle == "Quarterly" else range(1, 13)
		for period in periods:
			for year in (today.year, today.year + 1):
				try:
					from_date, to_date, due = get_period_dates(
						row.reporting_cycle, year, period
					)
				except (KeyError, ValueError):
					continue

				# Only periods that have ended and are due within the horizon.
				if to_date >= today or not (today <= due <= horizon):
					continue

				exists = frappe.db.exists(
					"ES BAS Return",
					{"company": row.company, "from_date": from_date,
					 "to_date": to_date, "docstatus": ["<", 2]},
				)
				if exists:
					continue

				_notify(row.company, from_date, to_date, due)


def _notify(company, from_date, to_date, due):
	recipients = _accounts_managers()
	if not recipients:
		return

	subject = f"BAS due {due} for {company}"
	message = (
		f"The BAS period {from_date} to {to_date} for {company} has closed "
		f"and is due on {due}. No return has been prepared yet."
	)

	for user in recipients:
		if frappe.db.exists(
			"Notification Log",
			{"subject": subject, "for_user": user, "document_name": company},
		):
			continue
		frappe.get_doc({
			"doctype": "Notification Log",
			"subject": subject,
			"email_content": message,
			"for_user": user,
			"type": "Alert",
			"document_type": "Company",
			"document_name": company,
		}).insert(ignore_permissions=True)

	frappe.db.commit()


def _accounts_managers():
	return frappe.get_all(
		"Has Role",
		filters={"role": "Accounts Manager", "parenttype": "User"},
		pluck="parent",
	)

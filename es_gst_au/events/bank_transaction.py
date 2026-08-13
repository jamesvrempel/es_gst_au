# Copyright (c) 2026, Enterprise Systems Australia and contributors
"""
Bank Transaction hooks.

Transactions created by an import batch are coded by the batch itself so
that progress is tracked and failures are visible. Transactions created
any other way (a manual entry, another importer, a bank feed) are coded
here, provided the company has opted into automatic posting.
"""

import frappe
from frappe.utils import cint


def after_insert(doc, method=None):
	if doc.get("es_import_batch"):
		# The batch owns posting for its own rows.
		return

	settings_name = frappe.db.get_value("ES Bank Import Settings", {"company": doc.company})
	if not settings_name:
		return

	settings = frappe.get_cached_doc("ES Bank Import Settings", settings_name)
	if not cint(settings.auto_submit):
		return

	frappe.enqueue(
		"es_gst_au.utils.vouchers.process_transaction",
		queue="short",
		txn=doc.name,
		submit=True,
		job_name=f"es-gst-code-{doc.name}",
	)

# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import json

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint, flt, get_link_to_form

from es_gst_au.utils.gst import money
from es_gst_au.utils.parsers import row_hash, sniff_and_parse
from es_gst_au.utils.rules import ACTION_IGNORE, classify, get_rules
from es_gst_au.utils.vouchers import get_gst_settings, get_settings, process_transaction

# Posting is done in chunks so that a large statement neither blocks a
# web worker nor loses everything to a single failure.
POST_CHUNK_SIZE = 50


class ESBankImportBatch(Document):
	def validate(self):
		if self.bank_account:
			account_company = frappe.db.get_value("Bank Account", self.bank_account, "company")
			if account_company and account_company != self.company:
				frappe.throw(
					_("Bank Account {0} belongs to {1}, not {2}.").format(
						frappe.bold(self.bank_account), frappe.bold(account_company), frappe.bold(self.company)
					)
				)
		self.recalculate_totals()

	def on_submit(self):
		if not self.rows:
			frappe.throw(_("Parse the statement before submitting."))
		self.create_bank_transactions()

	def on_cancel(self):
		self.ignore_linked_doctypes = ("GL Entry",)
		posted = [r for r in self.rows if r.voucher]
		if posted:
			frappe.throw(
				_("This batch has {0} posted vouchers. Cancel those first.").format(len(posted))
			)

	# ------------------------------------------------------------------
	# Parsing
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def parse_statement(self):
		"""Read the attached file into rows, flagging duplicates as we go."""
		if not self.import_file:
			frappe.throw(_("Attach a statement file first."))

		content = get_file_content(self.import_file)
		forced = None if self.bank_format in (None, "Auto Detect") else self.bank_format
		fmt, parsed = sniff_and_parse(content, self.import_file, forced)

		self.set("rows", [])
		seen_in_file = {}
		rules = get_rules(self.company)

		for line in parsed:
			# Two genuinely distinct transactions can share date, amount
			# and merchant. Where the bank gives no running balance we
			# disambiguate by occurrence index so they do not collapse.
			base = row_hash(
				self.bank_account, line["date"], line["description"],
				line["debit"], line["credit"], line.get("balance"),
			)
			occurrence = seen_in_file.get(base, 0)
			seen_in_file[base] = occurrence + 1
			fingerprint = base if occurrence == 0 else f"{base}:{occurrence}"

			decision = classify(
				self.company, line["description"], line["debit"], line["credit"], rules=rules
			)

			existing = frappe.db.get_value(
				"Bank Transaction", {"es_row_hash": fingerprint, "docstatus": ["<", 2]}, "name"
			)

			if existing:
				status, note = "Duplicate", existing
			elif not decision.matched:
				status, note = "Unmatched", None
			elif decision.action == ACTION_IGNORE:
				status, note = "Ignored", None
			else:
				status, note = "Pending", None

			self.append("rows", {
				"date": line["date"],
				"description": line["description"][:500],
				"debit": flt(line["debit"]),
				"credit": flt(line["credit"]),
				"balance": flt(line["balance"]) if line.get("balance") is not None else None,
				"row_hash": fingerprint,
				"status": status,
				"matched_rule": decision.rule if decision.matched else None,
				"account": decision.account if decision.matched else None,
				"gst_treatment": decision.gst_treatment if decision.matched else None,
				"bank_transaction": note if status == "Duplicate" else None,
			})

		if parsed:
			self.statement_from = min(l["date"] for l in parsed)
			self.statement_to = max(l["date"] for l in parsed)

		self.bank_format = fmt
		self.status = "Parsed"
		self.recalculate_totals()
		self.save()

		return {
			"format": fmt,
			"total": len(parsed),
			"duplicates": self.duplicate_rows,
			"unmatched": self.unmatched_rows,
		}

	def recalculate_totals(self):
		self.total_rows = len(self.rows)
		self.duplicate_rows = sum(1 for r in self.rows if r.status == "Duplicate")
		self.unmatched_rows = sum(1 for r in self.rows if r.status == "Unmatched")
		self.imported_rows = sum(1 for r in self.rows if r.bank_transaction and r.status != "Duplicate")
		self.posted_rows = sum(1 for r in self.rows if r.voucher)
		self.error_rows = sum(1 for r in self.rows if r.status == "Error")
		self.total_debits = money(sum(flt(r.debit) for r in self.rows))
		self.total_credits = money(sum(flt(r.credit) for r in self.rows))

	# ------------------------------------------------------------------
	# Bank Transactions
	# ------------------------------------------------------------------

	def create_bank_transactions(self):
		"""Create a Bank Transaction per non-duplicate row."""
		created = 0
		for row in self.rows:
			if row.status == "Duplicate" or row.bank_transaction:
				continue
			try:
				txn = frappe.new_doc("Bank Transaction")
				txn.update({
					"date": row.date,
					"bank_account": self.bank_account,
					"company": self.company,
					"deposit": flt(row.credit),
					"withdrawal": flt(row.debit),
					"description": row.description,
					"reference_number": self.name,
					"currency": frappe.db.get_value("Company", self.company, "default_currency"),
					"es_row_hash": row.row_hash,
					"es_import_batch": self.name,
				})
				txn.insert(ignore_permissions=True)
				txn.submit()
				row.db_set("bank_transaction", txn.name, update_modified=False)
				created += 1
			except Exception:
				row.db_set("status", "Error", update_modified=False)
				row.db_set("error", frappe.get_traceback(with_context=False)[:500], update_modified=False)
				frappe.log_error(title=f"ES GST AU: bank transaction failed ({self.name})")

		self.reload()
		self.recalculate_totals()
		self.status = "Imported"
		self.db_update()
		frappe.msgprint(
			_("Created {0} bank transactions. {1} duplicates skipped.").format(
				frappe.bold(created), frappe.bold(self.duplicate_rows)
			)
		)

	# ------------------------------------------------------------------
	# Posting
	# ------------------------------------------------------------------

	@frappe.whitelist()
	def post_vouchers(self, enqueue=True):
		"""
		Create and reconcile vouchers for every coded row.

		Runs in the background by default. Posting is idempotent: a row
		that already carries a voucher is skipped, so a re-run after a
		partial failure resumes rather than duplicating.
		"""
		if self.docstatus != 1:
			frappe.throw(_("Submit the batch before posting vouchers."))

		pending = [r for r in self.rows if r.bank_transaction and not r.voucher and r.status != "Ignored"]
		if not pending:
			frappe.msgprint(_("Nothing left to post."))
			return {"queued": 0}

		if enqueue:
			frappe.enqueue(
				"es_gst_au.es_gst_au.doctype.es_bank_import_batch.es_bank_import_batch._post_batch",
				queue="long", timeout=7200, batch=self.name, job_name=f"es-gst-post-{self.name}",
			)
			frappe.msgprint(
				_("Queued {0} rows for posting. Progress will update on this document.").format(
					frappe.bold(len(pending))
				)
			)
			return {"queued": len(pending)}

		return _post_batch(self.name)


def _post_batch(batch):
	"""Worker entry point. Kept module level so it is enqueueable."""
	doc = frappe.get_doc("ES Bank Import Batch", batch)
	settings = get_settings(doc.company)
	gst_settings = get_gst_settings(doc.company)

	posted = failed = 0
	for index, row in enumerate(doc.rows):
		if not row.bank_transaction or row.voucher or row.status == "Ignored":
			continue
		try:
			voucher_type, voucher = process_transaction(
				row.bank_transaction, settings, gst_settings,
				submit=cint(settings.auto_submit) or True,
			)
			if voucher:
				row.db_set("voucher_type", voucher_type, update_modified=False)
				row.db_set("voucher", voucher, update_modified=False)
				row.db_set("status", "Posted", update_modified=False)
				posted += 1
			else:
				row.db_set("status", "Unmatched", update_modified=False)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			row.db_set("status", "Error", update_modified=False)
			row.db_set("error", frappe.get_traceback(with_context=False)[:500], update_modified=False)
			frappe.db.commit()
			failed += 1
			frappe.log_error(title=f"ES GST AU: voucher failed for {row.bank_transaction}")

		if index % POST_CHUNK_SIZE == 0:
			frappe.publish_progress(
				percent=(index / max(len(doc.rows), 1)) * 100,
				title=_("Posting vouchers"), doctype=doc.doctype, docname=doc.name,
			)

	doc.reload()
	doc.recalculate_totals()
	doc.status = "Posted" if not failed and not doc.unmatched_rows else "Partially Posted"
	doc.db_update()
	frappe.db.commit()

	return {"posted": posted, "failed": failed}


def get_file_content(file_url):
	"""Read an attached File's bytes regardless of private/public storage."""
	file_doc = frappe.get_doc("File", {"file_url": file_url})
	return file_doc.get_content()


@frappe.whitelist()
def get_unmatched_summary(company, limit=50):
	"""
	Group unmatched descriptions so a single new rule can clear many rows.

	This is what makes the rule set converge: the largest unmatched
	cluster is always the most valuable rule to write next.
	"""
	frappe.has_permission("ES Bank Import Batch", throw=True)
	rows = frappe.db.sql(
		"""
		SELECT r.description, COUNT(*) AS occurrences,
		       SUM(r.debit) AS total_debit, SUM(r.credit) AS total_credit
		FROM `tabES Bank Import Batch Row` r
		INNER JOIN `tabES Bank Import Batch` b ON b.name = r.parent
		WHERE b.company = %(company)s AND r.status = 'Unmatched'
		GROUP BY r.description
		ORDER BY occurrences DESC, total_debit DESC
		LIMIT %(limit)s
		""",
		{"company": company, "limit": cint(limit)},
		as_dict=True,
	)
	return rows

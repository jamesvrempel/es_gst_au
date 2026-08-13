# Copyright (c) 2026, Enterprise Systems Australia and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import cint

VALID_SALES_LABELS = {"", "G1", "G2", "G3", "G4"}
VALID_PURCHASE_LABELS = {"", "G10", "G11", "G13", "G14", "G15"}


class ESGSTTreatment(Document):
	def validate(self):
		if (self.bas_sales_label or "") not in VALID_SALES_LABELS:
			frappe.throw(
				_("BAS Sales Label must be one of: {0}").format(
					", ".join(sorted(l for l in VALID_SALES_LABELS if l))
				)
			)
		if (self.bas_purchase_label or "") not in VALID_PURCHASE_LABELS:
			frappe.throw(
				_("BAS Purchase Label must be one of: {0}").format(
					", ".join(sorted(l for l in VALID_PURCHASE_LABELS if l))
				)
			)

		# A treatment that carries GST but cannot be claimed is a valid
		# combination (entertainment, for example), but claiming credit on
		# a treatment that carries no GST is not.
		if cint(self.claimable) and not cint(self.gst_applicable):
			frappe.msgprint(
				_("{0} carries no GST, so no input tax credit can arise from it.").format(
					frappe.bold(self.name or self.treatment_name)
				),
				indicator="orange", title=_("Check treatment"),
			)

	def on_trash(self):
		for doctype in ("ES Bank Rule", "Journal Entry", "Purchase Invoice", "Sales Invoice"):
			field = "gst_treatment" if doctype == "ES Bank Rule" else "es_gst_treatment"
			if not frappe.db.has_column(doctype, field):
				continue
			if frappe.db.exists(doctype, {field: self.name}):
				frappe.throw(
					_("Cannot delete {0}: it is in use by at least one {1}.").format(
						frappe.bold(self.name), doctype
					)
				)

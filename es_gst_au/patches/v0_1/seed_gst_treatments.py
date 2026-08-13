import frappe

from es_gst_au.install import seed_treatments


def execute():
	seed_treatments()
	frappe.db.commit()

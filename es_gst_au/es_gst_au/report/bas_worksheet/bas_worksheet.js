// Copyright (c) 2026, Enterprise Systems Australia and contributors
frappe.query_reports["BAS Worksheet"] = {
	filters: [
		{
			fieldname: "company", label: __("Company"), fieldtype: "Link",
			options: "Company", reqd: 1, default: frappe.defaults.get_user_default("Company"),
		},
		{
			fieldname: "from_date", label: __("From Date"), fieldtype: "Date", reqd: 1,
			default: frappe.datetime.add_months(frappe.datetime.get_today(), -3),
		},
		{
			fieldname: "to_date", label: __("To Date"), fieldtype: "Date", reqd: 1,
			default: frappe.datetime.get_today(),
		},
		{
			fieldname: "basis", label: __("Basis"), fieldtype: "Select",
			options: "Cash\nAccruals", default: "Cash",
		},
		{
			fieldname: "label", label: __("BAS Label"), fieldtype: "Select",
			options: ["", "G1", "G2", "G3", "G4", "G10", "G11", "G13", "G14", "G15"].join("\n"),
		},
	],
};

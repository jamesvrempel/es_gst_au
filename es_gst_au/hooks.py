# Copyright (c) 2026, Enterprise Systems Australia and contributors
from . import __version__ as app_version  # noqa

app_name = "es_gst_au"
app_title = "ES GST AU"
app_publisher = "Enterprise Systems Australia"
app_description = "Australian bank statement import, automated transaction coding, GST treatment and BAS reporting for ERPNext."
app_email = "support@enterprisesystems.com.au"
app_license = "MIT"
required_apps = ["erpnext"]

# Custom fields and default data are installed after app install/migrate.
after_install = "es_gst_au.install.after_install"
after_migrate = "es_gst_au.install.after_migrate"

_LOCK = "es_gst_au.es_gst_au.doctype.es_bas_return.es_bas_return.validate_period_not_locked"

doc_events = {
    "Bank Transaction": {
        "after_insert": "es_gst_au.events.bank_transaction.after_insert",
    },
    # Once a BAS return is submitted, its period is closed to new
    # postings. Amending the return reopens it.
    "Journal Entry": {"before_submit": _LOCK, "before_cancel": _LOCK},
    "Purchase Invoice": {"before_submit": _LOCK, "before_cancel": _LOCK},
    "Sales Invoice": {"before_submit": _LOCK, "before_cancel": _LOCK},
    "Payment Entry": {"before_submit": _LOCK, "before_cancel": _LOCK},
}

scheduler_events = {
    "daily": [
        "es_gst_au.tasks.retry_failed_postings",
    ],
    "cron": {
        # Check for BAS periods that have closed and need lodging.
        "0 6 * * *": ["es_gst_au.tasks.flag_due_bas_periods"],
    },
}

override_whitelisted_methods = {}

fixtures = [
    {"dt": "ES GST Treatment"},
]

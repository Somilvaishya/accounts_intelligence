"""The page that serves the reconciliation frontend. `yarn build` in frontend/ writes intelligence.html here."""

import frappe
from frappe import _

from accounts_intelligence.auth import SETTINGS

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/intelligence"
		raise frappe.Redirect
	if "System Manager" not in frappe.get_roles():
		frappe.throw(_("Only a System Manager can open Accounts Intelligence."), frappe.PermissionError)
	if frappe.db.get_single_value(SETTINGS, "mode") != "Server":
		frappe.throw(_("Reconciliation runs on the group server. This site is not set up as the server."))
	context.csrf_token = frappe.sessions.get_csrf_token()
	# The CSRF token is kept in the session, which a GET request does not save on its own.
	frappe.db.commit()  # nosemgrep
	return context

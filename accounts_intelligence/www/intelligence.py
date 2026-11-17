"""The page that serves the reconciliation frontend. `yarn build` in frontend/ writes intelligence.html here."""

import frappe

from accounts_intelligence import roles

no_cache = 1


def get_context(context):
	if frappe.session.user == "Guest":
		frappe.local.flags.redirect_location = "/login?redirect-to=/intelligence"
		raise frappe.Redirect
	# One of the app's roles, on the group server. A company site sends the user to its server's dashboard.
	roles.page_access()
	context.csrf_token = frappe.sessions.get_csrf_token()
	context.user_name = frappe.utils.get_fullname()
	context.user_id = frappe.session.user
	# The CSRF token is kept in the session, which a GET request does not save on its own.
	frappe.db.commit()  # nosemgrep
	return context

"""Who can use Accounts Intelligence.

- Accounts Intelligence Manager: everything in the app, without the rest of a System Manager's reach. The
  settings and keys, connecting and approving sites (auth/admin.py), the related-party register and the auth
  log, as well as the reconciliation.
- Accounts Intelligence User: the reconciliation on the group server (the dashboard, pairs, related parties,
  open items, sync health and the assistant), and reviewing open items. No settings or keys.
- A System Manager can do everything, as before."""

import frappe
from frappe import _

from accounts_intelligence.auth import SETTINGS

MANAGER_ROLE = "Accounts Intelligence Manager"
USER_ROLE = "Accounts Intelligence User"
MANAGERS = ("System Manager", MANAGER_ROLE)
ROLES = (*MANAGERS, USER_ROLE)
SETTINGS_ROUTE = "/app/accounts-intelligence-settings"


def check():
	"""Refuse anyone without one of the roles that can use the reconciliation."""
	frappe.only_for(ROLES)


def check_manager():
	"""Refuse anyone who cannot change the settings, keys or connected sites."""
	frappe.only_for(MANAGERS)


def _has(role_names) -> bool:
	return frappe.session.user != "Guest" and bool(set(role_names) & set(frappe.get_roles()))


def is_server() -> bool:
	return frappe.db.get_single_value(SETTINGS, "mode") == "Server"


def check_app_permission() -> bool:
	"""Whether the apps screen shows the tile: to anyone with one of the roles. It opens the dashboard, which
	on a company site is the group server's (dashboard_url)."""
	return _has(ROLES)


def dashboard_url() -> str | None:
	"""Where the dashboard is for this site: here on the group server, on the server a company site is
	connected to, or nowhere yet on a company site that is not connected."""
	if is_server():
		return "/intelligence"
	server_url = frappe.db.get_single_value(SETTINGS, "server_url")
	return f"{server_url.rstrip('/')}/intelligence" if server_url else None


def page_access():
	"""The /intelligence page's check. It is served on the group server only: a company site sends the user
	to its server's dashboard, where they sign in, or a manager to the settings while it is not connected."""
	if not _has(ROLES):
		frappe.throw(
			_("You need the {0} or {1} role to open Accounts Intelligence.").format(USER_ROLE, MANAGER_ROLE),
			frappe.PermissionError,
		)
	if is_server():
		return
	target = dashboard_url()
	if not target and _has(MANAGERS):
		target = SETTINGS_ROUTE
	if not target:
		frappe.throw(
			_("This site is not connected to the group server yet. Ask an Accounts Intelligence Manager.")
		)
	frappe.local.flags.redirect_location = target
	raise frappe.Redirect


def setup():
	"""Create the roles. Runs after install and every migrate, so it is safe to repeat, and leaves a role
	made by hand as it is."""
	for role in (MANAGER_ROLE, USER_ROLE):
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def users() -> list[str]:
	"""Users holding one of the roles, enabled or not."""
	return frappe.get_all(
		"Has Role", {"role": ("in", ROLES), "parenttype": "User"}, pluck="parent", distinct=True
	)

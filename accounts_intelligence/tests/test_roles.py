"""Tests for who can use Accounts Intelligence: System Managers, the Accounts Intelligence Manager role, which
runs the whole app, and the Accounts Intelligence User role, which sees the reconciliation but not the settings
or keys."""

from contextlib import contextmanager

import frappe

from accounts_intelligence import open_items, reconcile, roles
from accounts_intelligence.auth import SETTINGS, admin
from accounts_intelligence.sync import ENTITY, RELATED_PARTY, TRANSACTION
from accounts_intelligence.tests.test_auth import FrappeTestCase
from accounts_intelligence.tests.test_reconcile import two_companies
from accounts_intelligence.www import intelligence

MANAGER = "ai-role-manager@example.com"
USER = "ai-role-user@example.com"
NOBODY = "ai-no-role@example.com"
SERVER = "https://group.example.com"


@contextmanager
def checked():
	"""Frappe v15's only_for lets everyone through while tests run; switch that off so the roles really are
	checked here. v16 always checks."""
	in_test = frappe.flags.in_test
	frappe.flags.in_test = False
	try:
		yield
	finally:
		frappe.flags.in_test = in_test


def _user(email, role_list):
	"""The user with exactly these roles. Kept between runs rather than recreated: other apps add rows of
	their own for each new user."""
	if frappe.db.exists("User", email):
		user = frappe.get_doc("User", email)
	else:
		user = frappe.get_doc(
			{"doctype": "User", "email": email, "first_name": email.split("@")[0], "send_welcome_email": 0}
		).insert(ignore_permissions=True)
	user.set("roles", [{"role": role} for role in role_list])
	user.enabled = 1
	user.save(ignore_permissions=True)


def _page():
	"""Open /intelligence and return where it sent the user, or None when it served the dashboard."""
	frappe.local.flags.redirect_location = None
	try:
		intelligence.get_context(frappe._dict())
	except frappe.Redirect:
		return frappe.local.flags.redirect_location


class TestRoles(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		roles.setup()
		_user(MANAGER, [roles.MANAGER_ROLE])
		_user(USER, [roles.USER_ROLE])
		_user(NOBODY, [])
		two_companies()
		self.saved = frappe.db.get_value(SETTINGS, SETTINGS, ["mode", "server_url"], as_dict=True)
		frappe.db.set_single_value(SETTINGS, "mode", "Server")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.set_single_value(SETTINGS, self.saved)

	def _client(self, server_url):
		frappe.db.set_single_value(SETTINGS, {"mode": "Client", "server_url": server_url})

	def test_setup_creates_both_roles_once(self):
		roles.setup()
		for role in (roles.MANAGER_ROLE, roles.USER_ROLE):
			self.assertEqual(frappe.db.count("Role", {"name": role}), 1)
			self.assertTrue(frappe.db.get_value("Role", role, "desk_access"))

	def test_both_roles_use_the_reconciliation_and_see_the_tile(self):
		for email in (MANAGER, USER):
			frappe.set_user(email)
			with checked():
				self.assertIn("pairs", reconcile.dashboard())
				self.assertIn("items", open_items.items())
			self.assertTrue(roles.check_app_permission())
			self.assertIsNone(_page(), "the server serves the dashboard")
			self.assertTrue(frappe.has_permission(TRANSACTION, "read"))
			self.assertFalse(frappe.has_permission(TRANSACTION, "write"))

	def test_the_manager_runs_the_settings_and_the_register(self):
		frappe.set_user(MANAGER)
		with checked():
			roles.check_manager()
		self.assertTrue(frappe.has_permission(SETTINGS, "write"))
		self.assertTrue(frappe.has_permission(RELATED_PARTY, "create"))

	def test_the_user_does_not_reach_settings_or_keys(self):
		frappe.set_user(USER)
		self.assertFalse(frappe.has_permission(SETTINGS, "read"))
		self.assertFalse(frappe.has_permission(RELATED_PARTY, "write"))
		with checked(), self.assertRaises(frappe.PermissionError):
			admin.regenerate_key()

	def test_role_holders_can_be_assigned_open_items(self):
		users = [u["name"] for u in open_items.items()["users"]]
		self.assertIn(MANAGER, users)
		self.assertIn(USER, users)
		self.assertNotIn(NOBODY, users)

	def test_a_user_without_a_role_is_refused(self):
		frappe.set_user(NOBODY)
		with checked(), self.assertRaises(frappe.PermissionError):
			reconcile.dashboard()
		with self.assertRaises(frappe.PermissionError):
			intelligence.get_context(frappe._dict())
		self.assertFalse(roles.check_app_permission())
		self.assertFalse(frappe.has_permission(ENTITY, "read"))

	def test_a_company_site_opens_the_servers_dashboard(self):
		self._client(SERVER + "/")
		for email in (MANAGER, USER, "Administrator"):
			frappe.set_user(email)
			self.assertTrue(roles.check_app_permission())
			self.assertEqual(_page(), SERVER + "/intelligence")

	def test_a_company_site_not_yet_connected_sends_a_manager_to_the_settings(self):
		self._client(None)
		frappe.set_user(MANAGER)
		self.assertEqual(_page(), roles.SETTINGS_ROUTE)
		frappe.set_user(USER)
		with self.assertRaises(frappe.ValidationError):
			_page()

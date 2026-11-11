"""Tests for the Sync Health page: site states, warnings from the site's own result, and activity."""

import frappe
from frappe.utils import add_to_date, now_datetime

from accounts_intelligence import health
from accounts_intelligence.auth import ALLOWED_SITE, AUTH_LOG, audit, keys
from accounts_intelligence.sync import ENTITY
from accounts_intelligence.sync import server as sync_server
from accounts_intelligence.tests.test_auth import CLIENT, FrappeTestCase, _reset_settings, _row
from accounts_intelligence.tests.test_sync import _request

PAN = "AAACA1111A"
RESULT = "Sent 3 documents and 0 balances. Notes: Supplier SP1 is named like Beta Ltd, which the group server watches"


class TestSyncHealth(FrappeTestCase):
	def setUp(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		frappe.db.delete(ENTITY)
		frappe.db.delete(AUTH_LOG)
		frappe.get_doc(
			{"doctype": ENTITY, "pan": PAN, "company_name": "Alpha Ltd", "site_url": CLIENT}
		).insert(ignore_permissions=True)

	def _site(self):
		return next(s for s in health.sites()["sites"] if s["site_url"] == CLIENT)

	def _config(self, **data):
		companies = [{"company": "Alpha Ltd", "pan": PAN}]
		return sync_server.config_handler(_request({"companies": companies, **data}, row_name=_row().name))

	def test_the_sites_own_result_reaches_the_server_with_its_warnings(self):
		self._config(last_result=RESULT)
		self.assertEqual(_row().last_result, RESULT)
		site = self._site()
		self.assertEqual(site["state"], "Healthy")
		self.assertEqual(site["companies"], [{"pan": PAN, "name": "Alpha Ltd"}])
		self.assertEqual(
			site["warnings"], ["Supplier SP1 is named like Beta Ltd, which the group server watches"]
		)

		self._config()
		self.assertEqual(_row().last_result, RESULT, "an older client that sends no result changes nothing")

	def test_each_state_is_told_apart(self):
		row = _row().name
		cases = (
			({"last_sync_on": None}, "Never synced"),
			({"last_sync_on": add_to_date(now_datetime(), days=-2)}, "Late"),
			({"last_sync_on": now_datetime()}, "Healthy"),
			({"enabled": 0}, "Disabled"),
			({"status": "Revoked"}, "Revoked"),
		)
		for values, state in cases:
			frappe.db.set_value(ALLOWED_SITE, row, values)
			self.assertEqual(self._site()["state"], state, values)

	def test_a_failed_run_and_a_daily_check_that_differs_are_warnings(self):
		self.assertEqual(health.warnings("server unreachable", None), ["Last run: server unreachable"])
		self.assertEqual(health.warnings("Up to date.", "2026-11-12 10:00: all 33 months match"), [])
		self.assertEqual(
			health.warnings("Up to date.", "2026-11-12 10:00: 2 differ. ..."),
			["Daily check: 2026-11-12 10:00: 2 differ. ..."],
		)

	def test_activity_counts_accepted_calls_by_hour_and_rejected_ones_apart(self):
		audit.log("Sync", "Accepted", site_url=CLIENT)
		audit.log("Sync config", "Accepted", site_url=CLIENT)
		audit.log("Sync", "Rejected", site_url=CLIENT, reason="bad signature")
		site = self._site()
		self.assertEqual(len(site["activity"]), 24)
		self.assertEqual(site["activity"][-1], 2, "this hour is the last bar")
		self.assertEqual(site["rejected"], 1)

	def test_only_a_system_manager_can_read_it(self):
		in_test, frappe.flags.in_test = frappe.flags.in_test, False
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, health.sites)
		finally:
			frappe.set_user("Administrator")
			frappe.flags.in_test = in_test

"""Tests for the audit fixes: private key export (M1), approval bound to a fingerprint (M2), site URL
changes while connected (M3), rejection log flooding and endpoint caps (M4), Auth Log indexes (M5)."""

from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder

from accounts_intelligence import overrides
from accounts_intelligence.auth import ALLOWED_SITE, AUTH_LOG, SETTINGS, admin, audit, keys, server
from accounts_intelligence.tests.test_auth import (
	CLIENT,
	CONNECT,
	FrappeTestCase,
	_reset_settings,
	_row,
	clear_counters,
)


class TestPrivateKeyExport(FrappeTestCase):
	def test_hook_replaces_frappe_get_password(self):
		hooks = frappe.get_hooks("override_whitelisted_methods").get("frappe.client.get_password")
		self.assertIn("accounts_intelligence.overrides.get_password", hooks)

	def test_private_keys_cannot_be_read(self):
		for field in ("private_key", "pending_private_key"):
			with self.assertRaises(frappe.PermissionError, msg=field):
				overrides.get_password(SETTINGS, SETTINGS, field)

	def test_other_password_fields_still_work(self):
		with patch("accounts_intelligence.overrides.frappe_get_password", return_value="secret") as original:
			self.assertEqual(overrides.get_password("Email Account", "Support", "password"), "secret")
		original.assert_called_once_with("Email Account", "Support", "password")


class TestApprovalFingerprint(FrappeTestCase):
	def setUp(self):
		self.client_public = keys.generate_keypair()[1]
		_reset_settings(row_status="Pending", row_key=self.client_public)
		self.row = _row()

	def test_approve_with_the_fingerprint_shown(self):
		admin.server_approve([self.row.name], {self.row.name: self.row.fingerprint})
		self.assertEqual(_row().status, "Active")

	def test_key_changed_after_the_page_loaded_is_not_approved(self):
		shown = self.row.fingerprint
		swapped = keys.generate_keypair()[1]
		frappe.db.set_value(
			ALLOWED_SITE, self.row.name, {"public_key": swapped, "fingerprint": keys.fingerprint(swapped)}
		)
		with self.assertRaises(frappe.ValidationError):
			admin.server_approve([self.row.name], {self.row.name: shown})
		self.assertEqual(_row().status, "Pending")

	def test_approve_without_a_fingerprint_is_refused(self):
		with self.assertRaises(frappe.ValidationError):
			admin.server_approve([self.row.name])
		self.assertEqual(_row().status, "Pending")

	def test_revoke_and_reset_need_no_fingerprint(self):
		admin.server_revoke([self.row.name])
		self.assertEqual(_row().status, "Revoked")
		admin.server_reset([self.row.name])
		self.assertEqual(_row().status, "Pending")


class TestSiteURLChange(FrappeTestCase):
	def test_server_url_cannot_change_while_clients_are_connected(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		settings = frappe.get_single(SETTINGS)
		settings.site_url = "https://moved-server.example.com"
		self.assertRaises(frappe.ValidationError, settings.save)

	def test_client_url_cannot_change_while_connected(self):
		_reset_settings(row_status="Pending", row_key=None)
		frappe.db.delete(ALLOWED_SITE, {"parent": SETTINGS})
		frappe.db.set_single_value(
			SETTINGS,
			{
				"mode": "Client",
				"site_url": CLIENT,
				"server_url": "https://server.example.com",
				"server_public_key": keys.generate_keypair()[1],
				"connection_status": "Connected",
			},
		)
		settings = frappe.get_single(SETTINGS)
		settings.site_url = "https://moved-client.example.com"
		self.assertRaises(frappe.ValidationError, settings.save)

	def test_url_can_change_when_not_connected(self):
		_reset_settings(row_status="Pending", row_key=None)
		settings = frappe.get_single(SETTINGS)
		settings.site_url = "https://moved-server.example.com"
		settings.save()
		self.assertEqual(
			frappe.db.get_value(SETTINGS, SETTINGS, "site_url"), "https://moved-server.example.com"
		)


class TestFloodProtection(FrappeTestCase):
	def setUp(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		clear_counters()

	def tearDown(self):
		clear_counters()
		frappe.local.request = None

	def _rejected_rows(self):
		return frappe.db.count(AUTH_LOG, {"event": "Flood test", "result": "Rejected"})

	def test_repeated_rejections_write_only_a_few_rows(self):
		for i in range(25):
			audit.log_rejection("Flood test", f"https://attacker-{i}.example.com", "signature does not match")
		# Rows at the 1st and 10th occurrence only, although every request claimed a different site.
		self.assertEqual(self._rejected_rows(), 2)
		reasons = frappe.get_all(AUTH_LOG, {"event": "Flood test"}, pluck="reason", order_by="creation")
		self.assertIn("seen 10 times", reasons[-1])

	def test_endpoint_cap_answers_429_without_checking_the_request(self):
		frappe.local.request = EnvironBuilder(path=CONNECT, method="POST", data=b"{}").get_request()
		frappe.local.response = frappe._dict()
		with patch.object(server, "verify_request", side_effect=server.Rejected("test")) as verify:
			replies = [server.handle("Cap test", server.ping_handler, limit_per_minute=2) for _ in range(3)]
		self.assertEqual(replies[0]["error"], "not_authorised")
		self.assertEqual(replies[2], {"ok": False, "error": "rate_limited"})
		self.assertEqual(frappe.local.response.get("http_status_code"), 429)
		self.assertEqual(verify.call_count, 2)


class TestAuthLogIndexes(FrappeTestCase):
	def test_indexes_exist(self):
		# creation is indexed by Frappe itself because the DocType sorts by it; the other two are ours.
		table = "tabAccounts Intelligence Auth Log"
		for column in ("creation", "site_url", "result"):
			self.assertTrue(frappe.db.get_column_index(table, column), column)

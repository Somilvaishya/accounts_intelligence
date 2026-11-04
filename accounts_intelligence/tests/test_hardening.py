"""Tests for the audit fixes: private key export (M1), approval bound to a fingerprint (M2), site URL
changes while connected (M3), rejection log flooding and endpoint caps (M4), Auth Log indexes (M5),
and the low-severity items L1-L7."""

from unittest.mock import MagicMock, patch

import frappe
from frappe.utils.password import remove_encrypted_password
from werkzeug.test import EnvironBuilder

from accounts_intelligence import api, overrides
from accounts_intelligence.auth import (
	ALLOWED_SITE,
	AUTH_LOG,
	SETTINGS,
	admin,
	audit,
	keys,
	server,
	signing,
	transport,
)
from accounts_intelligence.tests.test_auth import (
	CLIENT,
	CONNECT,
	PING,
	SERVER,
	FrappeTestCase,
	_reset_settings,
	_row,
	clear_counters,
)

POST_SIGNED = "accounts_intelligence.auth.transport.post_signed"


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


def _connected_client():
	"""Settings of a client that is connected to SERVER, with no rotation in progress."""
	server_public = keys.generate_keypair()[1]
	frappe.db.delete(ALLOWED_SITE, {"parent": SETTINGS})
	frappe.db.set_single_value(
		SETTINGS,
		{
			"mode": "Client",
			"site_url": CLIENT,
			"server_url": SERVER,
			"allow_http_for_testing": 0,
			"server_public_key": server_public,
			"server_fingerprint": keys.fingerprint(server_public),
			"connection_status": "Connected",
			"public_key": None,
			"key_fingerprint": None,
			"pending_public_key": None,
		},
	)
	remove_encrypted_password(SETTINGS, SETTINGS, "pending_private_key")
	frappe.get_single(SETTINGS).save()
	return frappe.db.get_value(SETTINGS, SETTINGS, "public_key")


def _http_reply(status, location=None):
	return MagicMock(status_code=status, headers={"Location": location} if location else {})


class TestVerifyReadsOnlySettingsValues(FrappeTestCase):
	def setUp(self):
		self.client_private, self.client_public = keys.generate_keypair()
		_reset_settings(row_status="Active", row_key=self.client_public)

	def tearDown(self):
		frappe.local.request = None

	def test_signed_request_is_verified_without_loading_the_settings_document(self):
		headers = signing.sign_request(
			self.client_private, self.client_public, "POST", PING, SERVER, CLIENT, b"{}"
		)
		frappe.local.request = EnvironBuilder(
			path=PING, method="POST", headers=headers, data=b"{}"
		).get_request()
		with patch.object(frappe, "get_single", side_effect=AssertionError("loads the whole document")):
			request = server.verify_request()
		self.assertEqual(request.row.site_url, CLIENT)


class TestRotationAgainstAnOlderServer(FrappeTestCase):
	def setUp(self):
		self.public = _connected_client()

	def test_other_4xx_is_a_refusal_but_5xx_is_unknown(self):
		settings = admin._client_settings()
		with patch("requests.post", return_value=_http_reply(417)):
			self.assertRaises(transport.Refused, transport.post_signed, settings, "rotate_key", {})
		with patch("requests.post", return_value=_http_reply(502)):
			with self.assertRaises(transport.TransportError) as caught:
				transport.post_signed(settings, "rotate_key", {})
			self.assertNotIsInstance(caught.exception, transport.Refused)

	def test_server_without_rotate_key_discards_the_new_key(self):
		with patch(
			POST_SIGNED, side_effect=transport.Refused("https://server.example.com answered with HTTP 417")
		):
			result = admin.client_rotate_key()
		self.assertFalse(result["ok"])
		self.assertIn("older version", result["message"])
		self.assertIsNone(admin._pending_key_pair())
		self.assertEqual(frappe.db.get_value(SETTINGS, SETTINGS, "public_key"), self.public)
		self.assertNotIn("next_public_key", api.identity())


class TestResetClearsPendingRotation(FrappeTestCase):
	def test_reset_stops_publishing_the_next_key(self):
		_connected_client()
		admin._store_pending(*keys.generate_keypair())
		self.assertIn("next_public_key", api.identity())
		admin.client_reset()
		self.assertIsNone(admin._pending_key_pair())
		self.assertNotIn("next_public_key", api.identity())


class TestRedirectMessage(FrappeTestCase):
	def test_redirect_names_the_final_address(self):
		reply = _http_reply(301, "https://erp.example.com/api/method/accounts_intelligence.api.identity")
		with patch("requests.get", return_value=reply):
			with self.assertRaises(transport.TransportError) as caught:
				transport.fetch_identity("https://group.frappe.cloud")
		self.assertIn("redirects to https://erp.example.com", str(caught.exception))
		self.assertIn("final address", str(caught.exception))


class TestRemovedSitesAreLogged(FrappeTestCase):
	def test_deleting_an_allowed_site_writes_a_log_row(self):
		_reset_settings(row_status="Revoked", row_key=keys.generate_keypair()[1])
		frappe.db.delete(AUTH_LOG, {"event": "Site removed"})
		settings = frappe.get_single(SETTINGS)
		settings.allowed_sites = []
		settings.save()
		log = frappe.get_all(AUTH_LOG, {"event": "Site removed"}, ["site_url", "reason"])
		self.assertEqual(log, [{"site_url": CLIENT, "reason": "status was Revoked"}])

	def test_saving_without_changes_logs_nothing(self):
		_reset_settings(row_status="Pending", row_key=None)
		frappe.db.delete(AUTH_LOG, {"event": "Site removed"})
		frappe.get_single(SETTINGS).save()
		self.assertFalse(frappe.db.exists(AUTH_LOG, {"event": "Site removed"}))


class TestHandlerError(FrappeTestCase):
	def setUp(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		clear_counters()
		frappe.local.request = EnvironBuilder(
			path=PING, method="POST", headers={signing.HEADER_SITE: CLIENT}, data=b"{}"
		).get_request()
		frappe.local.response = frappe._dict()

	def tearDown(self):
		clear_counters()
		frappe.local.request = None

	def test_unexpected_error_is_logged_undone_and_hidden(self):
		row = _row()

		def crashing_handler(request):
			frappe.db.set_value(ALLOWED_SITE, row.name, "status", "Revoked")
			raise RuntimeError("secret detail")

		signed = server.SignedRequest(row=row, data={}, public_key=row.public_key, nonce="a" * 32)
		with patch.object(server, "verify_request", return_value=signed):
			reply = server.handle("Crash test", crashing_handler)

		self.assertEqual(reply, {"ok": False, "error": "server_error"})
		self.assertEqual(frappe.local.response.get("http_status_code"), 500)
		self.assertEqual(_row().status, "Active", "the handler's partial write must be undone")
		self.assertTrue(
			frappe.db.exists(AUTH_LOG, {"event": "Crash test", "result": "Error", "site_url": CLIENT})
		)
		self.assertTrue(frappe.db.exists("Error Log", {"method": "Accounts Intelligence: Crash test failed"}))

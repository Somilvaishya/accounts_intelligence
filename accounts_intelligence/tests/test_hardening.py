"""Tests for the audit fixes: private key export (M1), approval bound to a fingerprint (M2), site URL
changes while connected (M3), rejection log flooding and endpoint caps (M4), Auth Log indexes (M5),
and the low-severity items L1-L7 and L9 (Auth Log volume)."""

import socket
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
		# Rows written by the other tests of this class are not rolled back between tests.
		frappe.db.delete(AUTH_LOG, {"event": ("in", ("Cap test", "Flood test"))})

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

	def _handle(self, verified, handler=server.ping_handler, limit=2):
		"""Call handle with verification stubbed: `verified` is a request that passed, or None for refused."""
		frappe.local.request = EnvironBuilder(path=CONNECT, method="POST", data=b"{}").get_request()
		frappe.local.response = frappe._dict()
		verify = (
			{"return_value": verified}
			if verified
			else {"side_effect": server.Rejected("signature does not match")}
		)
		with (
			patch.object(server, "verify_request", **verify),
			patch.object(server, "get_decrypted_password", return_value="key"),
			patch.object(signing, "sign_response", return_value={"ok": True}),
		):
			return server.handle("Cap test", handler, limit_per_minute=limit)

	def _verified(self, site=CLIENT):
		row = frappe._dict(name="row", site_url=site)
		return server.SignedRequest(row=row, data={}, public_key=keys.generate_keypair()[1], nonce="a" * 32)

	def test_refused_requests_share_a_cap_and_are_not_logged_past_it(self):
		replies = [self._handle(None) for _ in range(4)]
		self.assertEqual([r["error"] for r in replies], ["not_authorised"] * 2 + ["rate_limited"] * 2)
		self.assertEqual(
			frappe.db.count(AUTH_LOG, {"event": "Cap test"}), 1, "one row, then collapsed or capped"
		)

	def test_a_flood_of_refused_requests_does_not_block_a_verified_site(self):
		for _ in range(5):
			self._handle(None)
		self.assertEqual(self._handle(self._verified()), {"ok": True}, "its own budget is untouched")

	def test_each_verified_site_has_its_own_budget(self):
		replies = [self._handle(self._verified()) for _ in range(3)]
		self.assertEqual(replies[2], {"ok": False, "error": "rate_limited"})
		self.assertEqual(frappe.local.response.get("http_status_code"), 429)
		self.assertEqual(self._handle(self._verified("https://other.example.com")), {"ok": True})

	def test_a_refusal_inside_the_handler_undoes_its_writes(self):
		def handler(request):
			frappe.db.set_single_value(SETTINGS, "sync_every", "1 day")
			raise server.Rejected("check is not valid")

		frappe.db.set_single_value(SETTINGS, "sync_every", "15 minutes")
		self.assertEqual(self._handle(self._verified(), handler)["error"], "not_authorised")
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "sync_every"), "15 minutes")


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


class TestPlainHTTPMessage(FrappeTestCase):
	def test_turning_http_off_lists_every_http_address_at_once(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		frappe.db.set_single_value(SETTINGS, "site_url", "http://127.0.0.1:8000")
		frappe.db.set_value(ALLOWED_SITE, _row().name, "site_url", "http://localhost:8001")
		settings = frappe.get_single(SETTINGS)
		settings.allow_http_for_testing = 0
		with self.assertRaises(frappe.ValidationError) as caught:
			settings.save()
		message = str(caught.exception)
		self.assertIn("http://127.0.0.1:8000", message)
		self.assertIn("http://localhost:8001", message)
		self.assertIn("Reset Selected", message, "a connected site must be told to reset first")


class TestAuthLogVolume(FrappeTestCase):
	"""L9: routine successes do not fill the Auth Log; refusals are kept."""

	def setUp(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		frappe.db.delete(AUTH_LOG)
		clear_counters()

	def tearDown(self):
		frappe.local.request = None

	def _handle(self, event, **kwargs):
		"""A request that passes verification, so only the logging decision is under test."""
		request = server.SignedRequest(
			row=frappe._dict(_row()), data={}, public_key=keys.generate_keypair()[1], nonce="a" * 32
		)
		frappe.local.request = EnvironBuilder(path=PING, method="POST", data=b"{}").get_request()
		frappe.local.response = frappe._dict()
		with (
			patch.object(server, "verify_request", return_value=request),
			patch.object(server, "get_decrypted_password", return_value="key"),
			patch.object(signing, "sign_response", return_value={"ok": True}),
		):
			return server.handle(event, server.ping_handler, **kwargs)

	def test_an_accepted_ping_is_not_logged_but_a_refused_one_is(self):
		self._handle("Ping", log_accepted=False)
		self.assertEqual(frappe.db.count(AUTH_LOG), 0, "Last Seen already records it")
		self._handle("Sync")
		self.assertEqual(frappe.db.count(AUTH_LOG, {"event": "Sync", "result": "Accepted"}), 1)

		frappe.local.request = EnvironBuilder(path=PING, method="POST", data=b"{}").get_request()
		frappe.local.response = frappe._dict()
		server.handle("Ping", server.ping_handler, log_accepted=False)
		self.assertEqual(
			frappe.db.count(AUTH_LOG, {"event": "Ping", "result": "Rejected"}),
			1,
			"refusals are always logged",
		)

	def test_old_routine_entries_are_cleared_and_everything_else_is_kept(self):
		for event, result in (
			("Sync", "Accepted"),
			("Sync config", "Accepted"),
			("Sync", "Rejected"),
			("Connect", "Accepted"),
		):
			audit.log(event, result, site_url=CLIENT)
		frappe.db.sql(f"update `tab{AUTH_LOG}` set creation = now() - interval 8 day")
		audit.log("Sync", "Accepted", site_url=CLIENT)

		audit.clear_routine_entries()
		left = frappe.get_all(AUTH_LOG, fields=["event", "result"])
		self.assertEqual(
			sorted((r.event, r.result) for r in left),
			[("Connect", "Accepted"), ("Sync", "Accepted"), ("Sync", "Rejected")],
			"old accepted sync rows go; refusals, other events and this week's rows stay",
		)


class TestOutboundDestination(FrappeTestCase):
	"""Outside local testing, the server only calls public addresses (a client controls its own DNS)."""

	def setUp(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])

	def _resolves_to(self, address):
		family = socket.AF_INET6 if ":" in address else socket.AF_INET
		return patch.object(
			socket, "getaddrinfo", return_value=[(family, socket.SOCK_STREAM, 6, "", (address, 443))]
		)

	def test_an_internal_address_is_refused_before_any_request(self):
		for address in (
			"10.0.0.5",
			"127.0.0.1",
			"169.254.169.254",
			"192.168.1.10",
			"100.64.0.1",
			"::1",
			"::ffff:10.0.0.5",
		):
			with self._resolves_to(address), patch.object(transport.requests, "get") as get:
				with self.assertRaises(transport.TransportError) as caught:
					transport.fetch_identity(CLIENT)
			self.assertIn("internal address", str(caught.exception), address)
			get.assert_not_called()

	def test_a_public_address_is_called(self):
		with self._resolves_to("93.184.215.14"):
			transport.check_destination(CLIENT)

	def test_local_testing_may_call_local_addresses(self):
		with self._resolves_to("127.0.0.1"), patch.object(transport.urls, "http_allowed", return_value=True):
			transport.check_destination("http://localhost:8001")

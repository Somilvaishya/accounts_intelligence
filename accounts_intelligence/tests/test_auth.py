import json
import time
from unittest.mock import patch

import frappe
from frappe.tests.utils import FrappeTestCase
from werkzeug.test import EnvironBuilder

from accounts_intelligence.auth import ALLOWED_SITE, AUTH_LOG, SETTINGS, keys, server, signing
from accounts_intelligence.auth.urls import InvalidURL, canonical_url

SERVER = "https://server.example.com"
CLIENT = "https://client.example.com"
PING = "/api/method/accounts_intelligence.api.ping"
CONNECT = "/api/method/accounts_intelligence.api.connect"


def _reset_settings(mode="Server", row_status="Active", row_key=None):
	"""Write settings straight to the database, bypassing the form guards, so each test starts clean."""
	frappe.db.delete(ALLOWED_SITE, {"parent": SETTINGS})
	frappe.db.set_single_value(
		SETTINGS,
		{
			"mode": mode,
			"site_url": SERVER,
			"allow_http_for_testing": 0,
			"server_url": None,
			"server_public_key": None,
			"server_fingerprint": None,
			"connection_status": "Not connected",
			"public_key": None,
			"key_fingerprint": None,
		},
	)
	frappe.get_doc(
		{
			"doctype": ALLOWED_SITE,
			"name": frappe.generate_hash(length=10),
			"parent": SETTINGS,
			"parenttype": SETTINGS,
			"parentfield": "allowed_sites",
			"idx": 1,
			"site_url": CLIENT,
			"enabled": 1,
			"status": row_status,
			"public_key": row_key,
			"fingerprint": keys.fingerprint(row_key) if row_key else None,
		}
	).db_insert()
	# One save through the controller creates this site's own key pair.
	frappe.get_single(SETTINGS).save()


def _row():
	return frappe.db.get_value(ALLOWED_SITE, {"parent": SETTINGS, "site_url": CLIENT}, "*", as_dict=True)


class TestCanonicalURL(FrappeTestCase):
	def test_normalises_case_slash_and_default_port(self):
		self.assertEqual(canonical_url(" HTTPS://ERP.Example.com/ "), "https://erp.example.com")
		self.assertEqual(canonical_url("https://erp.example.com:443"), "https://erp.example.com")
		self.assertEqual(canonical_url("https://erp.example.com:8443"), "https://erp.example.com:8443")

	def test_rejects_anything_but_a_bare_site_address(self):
		for bad in (
			"",
			"erp.example.com",
			"ftp://erp.example.com",
			"https://erp.example.com/app",
			"https://erp.example.com?x=1",
			"https://erp.example.com#top",
			"https://user:pw@erp.example.com",
			"https://-bad-.example.com",
			"https://erp.example.com:99999",
		):
			with self.assertRaises(InvalidURL, msg=bad):
				canonical_url(bad)

	def test_http_needs_the_switch_and_developer_mode(self):
		with self.assertRaises(InvalidURL):
			canonical_url("http://localhost:8000")
		with patch.dict(frappe.conf, {"developer_mode": 0}):
			with self.assertRaises(InvalidURL):
				canonical_url("http://localhost:8000", allow_http=True)
		with patch.dict(frappe.conf, {"developer_mode": 1}):
			self.assertEqual(canonical_url("http://localhost:8000", allow_http=True), "http://localhost:8000")


class TestSigning(FrappeTestCase):
	def setUp(self):
		self.private, self.public = keys.generate_keypair()

	def test_signature_detects_any_change(self):
		signature = keys.sign(self.private, b"message")
		self.assertTrue(keys.verify(self.public, b"message", signature))
		self.assertFalse(keys.verify(self.public, b"messagE", signature))
		self.assertFalse(keys.verify(keys.generate_keypair()[1], b"message", signature))
		self.assertFalse(keys.verify(self.public, b"message", "not base64!"))
		self.assertFalse(keys.verify("short", b"message", signature))
		self.assertFalse(keys.verify(None, b"message", None))

	def test_fingerprint_is_stable_and_grouped(self):
		fingerprint = keys.fingerprint(self.public)
		self.assertEqual(fingerprint, keys.fingerprint(self.public))
		self.assertRegex(fingerprint, r"^([0-9A-F]{4} ){7}[0-9A-F]{4}$")

	def test_response_is_bound_to_the_request_nonce(self):
		nonce = "a" * 32
		envelope = signing.sign_response(self.private, nonce, {"ok": True})
		self.assertEqual(signing.open_response(self.public, nonce, envelope), {"ok": True})
		with self.assertRaises(signing.BadSignature):
			signing.open_response(self.public, "b" * 32, envelope)
		with self.assertRaises(signing.BadSignature):
			signing.open_response(self.public, nonce, {**envelope, "payload": '{"ok":false}'})
		with self.assertRaises(signing.BadSignature):
			signing.open_response(keys.generate_keypair()[1], nonce, envelope)

	def test_old_response_is_refused(self):
		with patch("time.time", return_value=time.time() - 1000):
			envelope = signing.sign_response(self.private, "a" * 32, {"ok": True})
		with self.assertRaises(signing.BadSignature):
			signing.open_response(self.public, "a" * 32, envelope)


class TestVerifyRequest(FrappeTestCase):
	def setUp(self):
		self.client_private, self.client_public = keys.generate_keypair()
		_reset_settings(row_status="Active", row_key=self.client_public)

	def tearDown(self):
		frappe.local.request = None

	def _signed(self, body=b"{}", path=PING, audience=SERVER, site=CLIENT, private=None, public=None):
		return signing.sign_request(
			private or self.client_private, public or self.client_public, "POST", path, audience, site, body
		)

	def _send(self, headers, body=b"{}", path=PING):
		frappe.local.request = EnvironBuilder(
			path=path, method="POST", headers={**headers, "Content-Type": "application/json"}, data=body
		).get_request()

	def _assert_rejected(self, reason_part, connect=False):
		with self.assertRaises(server.Rejected) as caught:
			server.verify_request(connect=connect)
		self.assertIn(reason_part, caught.exception.reason)

	def test_valid_request_is_accepted(self):
		self._send(self._signed())
		request = server.verify_request()
		self.assertEqual(request.row.site_url, CLIENT)
		self.assertTrue(_row().last_seen)

	def test_replayed_request_is_refused(self):
		headers = self._signed()
		self._send(headers)
		server.verify_request()
		self._send(headers)
		self._assert_rejected("nonce was already used")

	def test_changed_body_is_refused(self):
		self._send(self._signed(body=b'{"amount":1}'), body=b'{"amount":2}')
		self._assert_rejected("signature does not match")

	def test_changed_path_is_refused(self):
		self._send(self._signed(path=PING), path=CONNECT)
		self._assert_rejected("signature does not match")

	def test_request_meant_for_another_server_is_refused(self):
		self._send(self._signed(audience="https://other.example.com"))
		self._assert_rejected("signature does not match")

	def test_old_request_is_refused(self):
		with patch("time.time", return_value=time.time() - 1000):
			headers = self._signed()
		self._send(headers)
		self._assert_rejected("timestamp is outside")

	def test_other_key_is_refused(self):
		other_private, other_public = keys.generate_keypair()
		self._send(self._signed(private=other_private, public=other_public))
		self._assert_rejected("key id does not match")

	def test_site_not_on_the_list_is_refused(self):
		self._send(self._signed(site="https://stranger.example.com"))
		self._assert_rejected("not on the allowed list")

	def test_disabled_pending_and_revoked_sites_are_refused(self):
		row = _row()
		frappe.db.set_value(ALLOWED_SITE, row.name, "enabled", 0)
		self._send(self._signed())
		self._assert_rejected("site is disabled")
		for status in ("Pending", "Revoked"):
			frappe.db.set_value(ALLOWED_SITE, row.name, {"enabled": 1, "status": status})
			self._send(self._signed())
			self._assert_rejected(f"site status is {status}")

	def test_missing_headers_are_refused(self):
		headers = self._signed()
		headers.pop(signing.HEADER_SIGNATURE)
		self._send(headers)
		self._assert_rejected("headers are missing")

	def test_client_mode_site_refuses_every_request(self):
		frappe.db.set_single_value(SETTINGS, "mode", "Client")
		self._send(self._signed())
		self._assert_rejected("not set up as a server")


class TestConnect(FrappeTestCase):
	def setUp(self):
		self.client_private, self.client_public = keys.generate_keypair()
		_reset_settings(row_status="Pending", row_key=None)

	def tearDown(self):
		frappe.local.request = None

	def _connect_request(self, public_key=None):
		body = json.dumps({"site_url": CLIENT, "public_key": public_key or self.client_public}).encode()
		headers = signing.sign_request(
			self.client_private, self.client_public, "POST", CONNECT, SERVER, CLIENT, body
		)
		frappe.local.request = EnvironBuilder(
			path=CONNECT, method="POST", headers={**headers, "Content-Type": "application/json"}, data=body
		).get_request()

	def _identity(self, **overrides):
		return {
			"protocol": "AI1",
			"mode": "Client",
			"site_url": CLIENT,
			"public_key": self.client_public,
			**overrides,
		}

	def test_connect_stores_the_key_after_checking_the_site_address(self):
		self._connect_request()
		request = server.verify_request(connect=True)
		with patch("accounts_intelligence.auth.transport.fetch_identity", return_value=self._identity()):
			result = server.connect_handler(request)
		self.assertEqual(result["status"], "Pending")
		row = _row()
		self.assertEqual(row.public_key, self.client_public)
		self.assertEqual(row.status, "Pending")

	def test_connect_is_refused_when_the_site_publishes_another_key(self):
		self._connect_request()
		request = server.verify_request(connect=True)
		other = keys.generate_keypair()[1]
		with patch(
			"accounts_intelligence.auth.transport.fetch_identity",
			return_value=self._identity(public_key=other),
		):
			with self.assertRaises(server.Rejected):
				server.connect_handler(request)
		self.assertFalse(_row().public_key)

	def test_connect_cannot_swap_the_key_of_an_active_site(self):
		other_public = keys.generate_keypair()[1]
		frappe.db.set_value(ALLOWED_SITE, _row().name, {"status": "Active", "public_key": other_public})
		self._connect_request()
		with self.assertRaises(server.Rejected) as caught:
			server.verify_request(connect=True)
		self.assertIn("different key", caught.exception.reason)

	def test_rejection_is_logged_and_answered_with_a_plain_401(self):
		frappe.local.request = EnvironBuilder(path=CONNECT, method="POST", data=b"{}").get_request()
		frappe.local.response = frappe._dict()
		reply = server.handle("Connect", server.connect_handler, connect=True)
		self.assertEqual(reply, {"ok": False, "error": "not_authorised"})
		self.assertEqual(frappe.local.response.get("http_status_code"), 401)
		log = frappe.get_last_doc(AUTH_LOG)
		self.assertEqual((log.event, log.result), ("Connect", "Rejected"))
		self.assertIn("headers are missing", log.reason)


class TestSettingsGuards(FrappeTestCase):
	def setUp(self):
		self.client_public = keys.generate_keypair()[1]
		_reset_settings(row_status="Active", row_key=self.client_public)

	def test_form_save_cannot_change_keys_or_status(self):
		settings = frappe.get_single(SETTINGS)
		own_key = settings.public_key
		settings.public_key = keys.generate_keypair()[1]
		settings.allowed_sites[0].status = "Revoked"
		settings.allowed_sites[0].public_key = keys.generate_keypair()[1]
		settings.save()
		self.assertEqual(frappe.db.get_single_value(SETTINGS, "public_key"), own_key)
		self.assertEqual((_row().status, _row().public_key), ("Active", self.client_public))

	def test_new_row_starts_pending_without_a_key(self):
		settings = frappe.get_single(SETTINGS)
		settings.append(
			"allowed_sites",
			{"site_url": "https://second.example.com", "status": "Active", "public_key": self.client_public},
		)
		settings.save()
		row = frappe.db.get_value(ALLOWED_SITE, {"site_url": "https://second.example.com"}, "*", as_dict=True)
		self.assertEqual((row.status, row.public_key), ("Pending", None))

	def test_changing_a_row_address_resets_it(self):
		settings = frappe.get_single(SETTINGS)
		settings.allowed_sites[0].site_url = "https://moved.example.com"
		settings.save()
		row = frappe.db.get_value(ALLOWED_SITE, {"site_url": "https://moved.example.com"}, "*", as_dict=True)
		self.assertEqual((row.status, row.public_key), ("Pending", None))

	def test_duplicate_or_own_address_is_refused(self):
		settings = frappe.get_single(SETTINGS)
		settings.append("allowed_sites", {"site_url": "HTTPS://Client.Example.com/"})
		self.assertRaises(frappe.ValidationError, settings.save)
		settings = frappe.get_single(SETTINGS)
		settings.append("allowed_sites", {"site_url": SERVER})
		self.assertRaises(frappe.ValidationError, settings.save)

	def test_mode_cannot_change_while_sites_are_connected(self):
		settings = frappe.get_single(SETTINGS)
		settings.mode = "Client"
		settings.server_url = "https://group.example.com"
		self.assertRaises(frappe.ValidationError, settings.save)

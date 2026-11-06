import json
from unittest.mock import patch

import frappe
from frappe.utils.password import get_decrypted_password, remove_encrypted_password
from werkzeug.test import EnvironBuilder

from accounts_intelligence import api
from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS, admin, keys, server, signing, transport
from accounts_intelligence.tests.test_auth import (
	CLIENT,
	PING,
	SERVER,
	FrappeTestCase,
	_reset_settings,
	_row,
)

ROTATE = "/api/method/accounts_intelligence.api.rotate_key"
POST_SIGNED = "accounts_intelligence.auth.transport.post_signed"
FETCH_IDENTITY = "accounts_intelligence.auth.transport.fetch_identity"


def _send(private, public, path, body: bytes, nonce=None):
	headers = signing.sign_request(private, public, "POST", path, SERVER, CLIENT, body, nonce=nonce)
	frappe.local.request = EnvironBuilder(
		path=path, method="POST", headers={**headers, "Content-Type": "application/json"}, data=body
	).get_request()


def _own_key_pair():
	private = get_decrypted_password(SETTINGS, SETTINGS, "private_key")
	public = frappe.db.get_value(SETTINGS, SETTINGS, "public_key")
	return private, public


def _key_pair_matches(private, public) -> bool:
	return keys.verify(public, b"check", keys.sign(private, b"check"))


class TestRotateHandler(FrappeTestCase):
	"""The server side: when may an Active site swap its key without a new approval."""

	def setUp(self):
		self.old_private, self.old_public = keys.generate_keypair()
		self.new_private, self.new_public = keys.generate_keypair()
		_reset_settings(row_status="Active", row_key=self.old_public)

	def tearDown(self):
		frappe.local.request = None

	def _rotate_request(self, proof_key=None, proof_nonce=None, new_public=None):
		new_public = new_public or self.new_public
		nonce = signing.new_nonce()
		statement = signing.rotation_statement(CLIENT, self.old_public, new_public, proof_nonce or nonce)
		body = json.dumps(
			{
				"new_public_key": new_public,
				"new_key_signature": keys.sign(proof_key or self.new_private, statement),
			}
		).encode()
		_send(self.old_private, self.old_public, ROTATE, body, nonce=nonce)
		return server.verify_request()

	def _identity(self, **overrides):
		return {
			"protocol": "AI1",
			"mode": "Client",
			"site_url": CLIENT,
			"public_key": self.old_public,
			"next_public_key": self.new_public,
			**overrides,
		}

	def test_rotation_switches_the_key_and_keeps_the_site_active(self):
		request = self._rotate_request()
		with patch(FETCH_IDENTITY, return_value=self._identity()):
			result = server.rotate_handler(request)
		self.assertEqual(result["fingerprint"], keys.fingerprint(self.new_public))
		row = _row()
		self.assertEqual((row.status, row.public_key), ("Active", self.new_public))
		self.assertTrue(row.key_rotated_on)

		_send(self.old_private, self.old_public, PING, b"{}")
		with self.assertRaises(server.Rejected):
			server.verify_request()
		_send(self.new_private, self.new_public, PING, b"{}")
		self.assertEqual(server.verify_request().public_key, self.new_public)

	def test_old_key_alone_cannot_rotate_without_the_site_address(self):
		# A thief with the old private key cannot publish a key at the real site's URL.
		request = self._rotate_request()
		with patch(FETCH_IDENTITY, return_value=self._identity(next_public_key=None)):
			with self.assertRaises(server.Rejected) as caught:
				server.rotate_handler(request)
		self.assertIn("does not publish this new key", caught.exception.reason)
		self.assertEqual(_row().public_key, self.old_public)

	def test_new_key_must_sign_the_rotation(self):
		other_private = keys.generate_keypair()[0]
		request = self._rotate_request(proof_key=other_private)
		with self.assertRaises(server.Rejected) as caught:
			server.rotate_handler(request)
		self.assertIn("did not sign", caught.exception.reason)

	def test_proof_is_bound_to_the_request(self):
		request = self._rotate_request(proof_nonce=signing.new_nonce())
		with self.assertRaises(server.Rejected):
			server.rotate_handler(request)

	def test_same_key_is_refused(self):
		request = self._rotate_request(proof_key=self.old_private, new_public=self.old_public)
		with self.assertRaises(server.Rejected) as caught:
			server.rotate_handler(request)
		self.assertIn("same as the current key", caught.exception.reason)


class TestClientKeyActions(FrappeTestCase):
	"""The client side: Rotate Key and Regenerate Key on the settings form."""

	def setUp(self):
		self.server_public = keys.generate_keypair()[1]
		frappe.db.delete(ALLOWED_SITE, {"parent": SETTINGS})
		frappe.db.set_single_value(
			SETTINGS,
			{
				"mode": "Client",
				"site_url": CLIENT,
				"server_url": SERVER,
				"allow_http_for_testing": 0,
				"server_public_key": self.server_public,
				"server_fingerprint": keys.fingerprint(self.server_public),
				"connection_status": "Connected",
				"public_key": None,
				"key_fingerprint": None,
				"pending_public_key": None,
			},
		)
		remove_encrypted_password(SETTINGS, SETTINGS, "pending_private_key")
		frappe.get_single(SETTINGS).save()
		self.old_private, self.old_public = _own_key_pair()

	def _server_reply(self, new_public):
		return {
			"ok": True,
			"status": "Active",
			"for_site": CLIENT,
			"fingerprint": keys.fingerprint(new_public),
		}

	def test_rotate_key_switches_after_the_server_accepts(self):
		seen = {}

		def fake_post(settings, method, data, nonce=None, key_pair=None):
			seen["data"], seen["nonce"] = data, nonce
			seen["published"] = api.identity().get("next_public_key")
			return self._server_reply(data["new_public_key"])

		with patch(POST_SIGNED, side_effect=fake_post):
			result = admin.client_rotate_key()

		new_public = seen["data"]["new_public_key"]
		self.assertTrue(result["ok"])
		self.assertEqual(
			seen["published"], new_public, "the new key must be published while the server checks"
		)
		statement = signing.rotation_statement(CLIENT, self.old_public, new_public, seen["nonce"])
		self.assertTrue(keys.verify(new_public, statement, seen["data"]["new_key_signature"]))

		private, public = _own_key_pair()
		self.assertEqual(public, new_public)
		self.assertTrue(_key_pair_matches(private, public))
		self.assertNotIn("next_public_key", api.identity())
		self.assertEqual(frappe.db.get_value(SETTINGS, SETTINGS, "connection_status"), "Connected")

	def test_refused_rotation_keeps_the_current_key(self):
		with patch(POST_SIGNED, side_effect=transport.NotAuthorised("refused")):
			result = admin.client_rotate_key()
		self.assertFalse(result["ok"])
		self.assertEqual(_own_key_pair()[1], self.old_public)
		self.assertIsNone(admin._pending_key_pair())

	def test_interrupted_rotation_finishes_on_the_next_click(self):
		with patch(POST_SIGNED, side_effect=transport.TransportError("timeout")):
			result = admin.client_rotate_key()
		self.assertFalse(result["ok"])
		pending = admin._pending_key_pair()
		self.assertIsNotNone(pending, "an unconfirmed new key must be kept")
		self.assertEqual(_own_key_pair()[1], self.old_public)

		# The server did switch: a ping signed with the pending key is accepted.
		with patch(POST_SIGNED, return_value={"ok": True, "for_site": CLIENT}) as post:
			result = admin.client_rotate_key()
		self.assertTrue(result["ok"])
		self.assertEqual(post.call_args.kwargs["key_pair"], pending)
		self.assertEqual(_own_key_pair()[1], pending[1])
		self.assertIsNone(admin._pending_key_pair())

	def test_interrupted_rotation_is_not_discarded_while_the_server_is_unreachable(self):
		with patch(POST_SIGNED, side_effect=transport.TransportError("timeout")):
			admin.client_rotate_key()
			pending = admin._pending_key_pair()
			result = admin.client_rotate_key()
		self.assertFalse(result["ok"])
		self.assertEqual(admin._pending_key_pair(), pending)

	def test_regenerate_on_a_client_disconnects_it(self):
		result = admin.regenerate_key()
		private, public = _own_key_pair()
		self.assertNotEqual(public, self.old_public)
		self.assertTrue(_key_pair_matches(private, public))
		self.assertEqual(result["fingerprint"], keys.fingerprint(public))
		self.assertEqual(frappe.db.get_value(SETTINGS, SETTINGS, "connection_status"), "Not connected")


class TestServerRegenerate(FrappeTestCase):
	def test_regenerate_on_the_server_replaces_its_key(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		old_public = _own_key_pair()[1]
		admin.regenerate_key()
		private, public = _own_key_pair()
		self.assertNotEqual(public, old_public)
		self.assertTrue(_key_pair_matches(private, public))
		self.assertEqual(_row().status, "Active", "client approvals stay valid")

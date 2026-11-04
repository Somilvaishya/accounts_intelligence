"""Outbound HTTPS calls between sites. Redirects are never followed and replies are size-capped."""

import json

import requests
from frappe import _

from accounts_intelligence.auth import keys, signing

IDENTITY_PATH = "/api/method/accounts_intelligence.api.identity"
METHOD_PATH = "/api/method/accounts_intelligence.api.{0}"
TIMEOUT = 15
MAX_IDENTITY = 64 * 1024
MAX_REPLY = 1024 * 1024


class TransportError(Exception):
	pass


class NotAuthorised(TransportError):
	pass


def fetch_identity(base_url: str) -> dict:
	"""Read a site's published public identity over HTTPS."""
	try:
		response = requests.get(
			base_url + IDENTITY_PATH,
			timeout=TIMEOUT,
			allow_redirects=False,
			stream=True,
			headers={"Accept": "application/json"},
		)
	except requests.RequestException as e:
		raise TransportError(_("Could not reach {0} ({1})").format(base_url, e.__class__.__name__))
	if response.status_code != 200:
		raise TransportError(_("{0} answered with HTTP {1}").format(base_url, response.status_code))
	data = _json_message(response, MAX_IDENTITY, base_url)
	if not isinstance(data, dict) or data.get("protocol") != signing.PROTOCOL:
		raise TransportError(_("{0} is not running Accounts Intelligence").format(base_url))
	if not keys.is_valid_public_key(data.get("public_key")):
		raise TransportError(_("{0} did not publish a valid key").format(base_url))
	return data


def post_signed(settings, method: str, data: dict, nonce=None, key_pair=None) -> dict:
	"""Send a signed request to the server and return its verified reply.

	`settings` carries server_url, server_public_key, site_url, public_key and the decrypted `private`.
	`key_pair` (private, public) signs with a different key, used to check a key that is mid-rotation.
	"""
	private_key, public_key = key_pair or (settings.private, settings.public_key)
	path = METHOD_PATH.format(method)
	body = json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
	headers = signing.sign_request(
		private_key, public_key, "POST", path, settings.server_url, settings.site_url, body, nonce=nonce
	)
	try:
		response = requests.post(
			settings.server_url + path,
			data=body,
			headers={**headers, "Content-Type": "application/json", "Accept": "application/json"},
			timeout=TIMEOUT,
			allow_redirects=False,
			stream=True,
		)
	except requests.RequestException as e:
		raise TransportError(_("Could not reach {0} ({1})").format(settings.server_url, e.__class__.__name__))
	if response.status_code == 401:
		raise NotAuthorised(_("The server refused the request"))
	if response.status_code == 429:
		raise TransportError(_("The server is rate limiting this site. Try again in a minute."))
	if response.status_code != 200:
		raise TransportError(
			_("{0} answered with HTTP {1}").format(settings.server_url, response.status_code)
		)
	envelope = _json_message(response, MAX_REPLY, settings.server_url)
	try:
		return signing.open_response(settings.server_public_key, headers[signing.HEADER_NONCE], envelope)
	except signing.BadSignature as e:
		raise TransportError(_("The server's reply could not be verified: {0}").format(e))


def _json_message(response, limit: int, base_url: str):
	raw = response.raw.read(limit + 1, decode_content=True)
	response.close()
	if len(raw) > limit:
		raise TransportError(_("{0} sent a reply that is too large").format(base_url))
	try:
		return json.loads(raw).get("message")
	except (ValueError, AttributeError):
		raise TransportError(_("{0} did not answer with JSON").format(base_url))

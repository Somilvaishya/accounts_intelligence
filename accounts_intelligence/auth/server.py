"""Server side of protocol AI1. Every signed request is checked here before any handler runs.

There are no users or API keys: the only credential is a signature made with the private key of a site
on the allowed list. Every rejection gets the same 401 reply; the reason goes to the auth log only.
"""

import json
from dataclasses import dataclass

import frappe
from frappe.utils import get_datetime, now_datetime
from frappe.utils.password import get_decrypted_password

from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS, audit, keys, signing, transport
from accounts_intelligence.auth.urls import InvalidURL, canonical_url

MAX_BODY = 1024 * 1024
LAST_SEEN_EVERY = 60
ROW_FIELDS = ["name", "site_url", "enabled", "status", "public_key", "last_seen"]


class Rejected(Exception):
	def __init__(self, reason: str):
		super().__init__(reason)
		self.reason = reason


@dataclass
class SignedRequest:
	row: dict
	data: dict
	public_key: str
	nonce: str


def handle(event: str, handler, connect=False):
	"""Verify the current request, run the handler, and sign its result for the caller."""
	claimed_site = (frappe.get_request_header(signing.HEADER_SITE) or "")[:140]
	try:
		request = verify_request(connect=connect)
		private_key = get_decrypted_password(SETTINGS, SETTINGS, "private_key", raise_exception=False)
		if not private_key:
			raise Rejected("this server has no private key")
		result = handler(request)
	except Rejected as e:
		audit.log(event, "Rejected", site_url=claimed_site, reason=e.reason)
		return deny()
	audit.log(
		event, "Accepted", site_url=request.row.site_url, fingerprint=keys.fingerprint(request.public_key)
	)
	return signing.sign_response(private_key, request.nonce, {**result, "for_site": request.row.site_url})


def deny():
	frappe.local.response["http_status_code"] = 401
	return {"ok": False, "error": "not_authorised"}


def verify_request(connect=False) -> SignedRequest:
	settings = frappe.get_single(SETTINGS)
	if settings.mode != "Server" or not settings.site_url:
		raise Rejected("this site is not set up as a server")
	request = frappe.request
	if request.method != "POST":
		raise Rejected("only POST is accepted")
	if (request.content_length or 0) > MAX_BODY:
		raise Rejected("body is too large")

	site, key, timestamp, nonce, signature = (
		frappe.get_request_header(h) or ""
		for h in (
			signing.HEADER_SITE,
			signing.HEADER_KEY,
			signing.HEADER_TIMESTAMP,
			signing.HEADER_NONCE,
			signing.HEADER_SIGNATURE,
		)
	)
	if not all((site, key, timestamp, nonce, signature)):
		raise Rejected("signature headers are missing")
	try:
		if canonical_url(site, allow_http=settings.allow_http_for_testing) != site:
			raise Rejected("site address is not in canonical form")
	except InvalidURL:
		raise Rejected("site address is not valid")
	try:
		timestamp = int(timestamp)
	except ValueError:
		raise Rejected("timestamp is not a number")
	if not signing.within_window(timestamp):
		raise Rejected("timestamp is outside the allowed window")
	if not signing.NONCE_RE.match(nonce):
		raise Rejected("nonce is not valid")

	row = frappe.db.get_value(
		ALLOWED_SITE, {"parent": SETTINGS, "parenttype": SETTINGS, "site_url": site}, ROW_FIELDS, as_dict=True
	)
	if not row:
		raise Rejected("site is not on the allowed list")
	if not row.enabled:
		raise Rejected("site is disabled")

	body = request.get_data(cache=True)
	if len(body) > MAX_BODY:
		raise Rejected("body is too large")
	try:
		data = json.loads(body or b"{}")
	except ValueError:
		raise Rejected("body is not JSON")
	if not isinstance(data, dict):
		raise Rejected("body is not a JSON object")

	if connect:
		# Before approval the server has no key on file, so the request proves possession of the key it
		# carries. The connect handler then checks that the same key is published at the site's address.
		public_key = data.get("public_key")
		if not keys.is_valid_public_key(public_key):
			raise Rejected("connect request carries no valid public key")
		if row.status == "Revoked":
			raise Rejected("site is revoked")
		if row.status == "Active" and public_key != row.public_key:
			raise Rejected("site is already connected with a different key")
	else:
		if row.status != "Active":
			raise Rejected(f"site status is {row.status}")
		public_key = row.public_key

	if key != keys.key_id(public_key):
		raise Rejected("key id does not match")
	message = signing.request_message("POST", request.path, settings.site_url, site, timestamp, nonce, body)
	if not keys.verify(public_key, message, signature):
		raise Rejected("signature does not match")
	if not claim_nonce(site, nonce):
		raise Rejected("nonce was already used")

	_touch(row)
	return SignedRequest(row=row, data=data, public_key=public_key, nonce=nonce)


def claim_nonce(site: str, nonce: str) -> bool:
	"""Atomically mark a nonce as used. False if it was seen within the replay window."""
	key = frappe.cache.make_key(f"accounts_intelligence:nonce:{site}:{nonce}")
	return bool(frappe.cache.set(key, 1, ex=signing.NONCE_TTL, nx=True))


def _touch(row):
	now = now_datetime()
	if not row.last_seen or (now - get_datetime(row.last_seen)).total_seconds() > LAST_SEEN_EVERY:
		frappe.db.set_value(ALLOWED_SITE, row.name, "last_seen", now, update_modified=False)


def ping_handler(request: SignedRequest) -> dict:
	return {"ok": True, "status": "Active", "server_time": str(now_datetime())}


def connect_handler(request: SignedRequest) -> dict:
	row = request.row
	if request.data.get("site_url") != row.site_url:
		raise Rejected("site address in the body does not match the header")
	if row.status == "Active":
		return {"ok": True, "status": "Active"}
	try:
		identity = transport.fetch_identity(row.site_url)
	except transport.TransportError as e:
		raise Rejected(f"could not confirm the key at the site address: {e}")
	if (
		identity.get("mode") != "Client"
		or identity.get("site_url") != row.site_url
		or identity.get("public_key") != request.public_key
	):
		raise Rejected("the key published at the site address does not match the request")
	frappe.db.set_value(
		ALLOWED_SITE,
		row.name,
		{
			"public_key": request.public_key,
			"fingerprint": keys.fingerprint(request.public_key),
			"requested_on": now_datetime(),
		},
		update_modified=False,
	)
	return {"ok": True, "status": "Pending"}

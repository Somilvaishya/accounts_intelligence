"""Endpoints other sites call. They need no login: requests are authenticated by signature (auth/server.py)."""

import frappe
from frappe.rate_limiter import rate_limit

from accounts_intelligence.auth import SETTINGS, keys, server, signing


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=30, seconds=60)
def identity():
	"""This site's public identity. Only public information: the address, mode and public key."""
	settings = frappe.db.get_value(SETTINGS, SETTINGS, ["mode", "site_url", "public_key"], as_dict=True)
	if not settings or not settings.mode or not settings.site_url or not settings.public_key:
		frappe.local.response["http_status_code"] = 404
		return {"ok": False}
	return {
		"protocol": signing.PROTOCOL,
		"site_url": settings.site_url,
		"mode": settings.mode,
		"public_key": settings.public_key,
		"fingerprint": keys.fingerprint(settings.public_key),
	}


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60)
def connect():
	return server.handle("Connect", server.connect_handler, connect=True)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60)
def ping():
	return server.handle("Ping", server.ping_handler)

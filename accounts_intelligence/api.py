"""Endpoints other sites call. They need no login: requests are authenticated by signature (auth/server.py)."""

import frappe
from frappe.rate_limiter import rate_limit

from accounts_intelligence.auth import SETTINGS, keys, server, signing
from accounts_intelligence.sync import server as sync_server


@frappe.whitelist(allow_guest=True, methods=["GET"])
@rate_limit(limit=30, seconds=60)
def identity():
	"""This site's public identity. Only public information: the address, mode and public key.

	During a key rotation a client also publishes the key it is about to switch to, which is how the
	server confirms that the new key really belongs to this site.
	"""
	if server.over_limit("Identity", 300):
		return server.throttled()
	settings = frappe.db.get_value(
		SETTINGS, SETTINGS, ["mode", "site_url", "public_key", "pending_public_key"], as_dict=True
	)
	if not settings or not settings.mode or not settings.site_url or not settings.public_key:
		frappe.local.response["http_status_code"] = 404
		return {"ok": False}
	result = {
		"protocol": signing.PROTOCOL,
		"site_url": settings.site_url,
		"mode": settings.mode,
		"public_key": settings.public_key,
		"fingerprint": keys.fingerprint(settings.public_key),
	}
	if settings.mode == "Client" and settings.pending_public_key:
		result["next_public_key"] = settings.pending_public_key
	return result


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60)
def connect():
	return server.handle("Connect", server.connect_handler, connect=True, limit_per_minute=60)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=60, seconds=60)
def ping():
	# A ping that works only says the site is there, which its Last Seen already records.
	return server.handle("Ping", server.ping_handler, limit_per_minute=600, log_accepted=False)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=10, seconds=60)
def rotate_key():
	return server.handle("Rotate key", server.rotate_handler, limit_per_minute=60)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=30, seconds=60)
def sync_config():
	return server.handle("Sync config", sync_server.config_handler, limit_per_minute=120)


@frappe.whitelist(allow_guest=True, methods=["POST"])
@rate_limit(limit=300, seconds=60)
def sync():
	return server.handle("Sync", sync_server.sync_handler, limit_per_minute=1200)

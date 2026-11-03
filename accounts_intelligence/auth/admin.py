"""Admin actions behind the settings form buttons. System Manager only."""

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password

from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS, audit, keys, transport

CLIENT_STATUS = {"Pending": "Waiting for approval", "Active": "Connected"}
RESET_ROW = {
	"status": "Pending",
	"public_key": None,
	"fingerprint": None,
	"requested_on": None,
	"approved_on": None,
	"approved_by": None,
	"last_seen": None,
}


# ── client ──────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def client_connect():
	settings = _client_settings()
	try:
		identity = transport.fetch_identity(settings.server_url)
	except transport.TransportError as e:
		return _client_failed("Client connect", settings, str(e))
	if identity.get("mode") != "Server" or identity.get("site_url") != settings.server_url:
		return _client_failed(
			"Client connect",
			settings,
			_("{0} is not set up as an Accounts Intelligence server at this address.").format(
				settings.server_url
			),
		)
	if settings.server_public_key and identity["public_key"] != settings.server_public_key:
		return _client_failed(
			"Client connect",
			settings,
			_(
				"The server's key has changed since this site first connected. Do not continue unless the "
				"server's admin confirms they replaced it. Then use Reset connection and connect again."
			),
		)
	settings.server_public_key = identity["public_key"]
	settings.server_fingerprint = keys.fingerprint(identity["public_key"])
	frappe.db.set_single_value(
		SETTINGS,
		{"server_public_key": settings.server_public_key, "server_fingerprint": settings.server_fingerprint},
	)
	return _client_call(
		"Client connect",
		settings,
		"connect",
		{"site_url": settings.site_url, "public_key": settings.public_key},
	)


@frappe.whitelist(methods=["POST"])
def client_test():
	settings = _client_settings()
	if not settings.server_public_key:
		frappe.throw(_("Connect to the server first."))
	return _client_call("Client test", settings, "ping", {})


@frappe.whitelist(methods=["POST"])
def client_reset():
	frappe.only_for("System Manager")
	frappe.db.set_single_value(
		SETTINGS,
		{
			"server_public_key": None,
			"server_fingerprint": None,
			"connection_status": "Not connected",
			"last_connected_on": None,
			"last_error": None,
		},
	)
	audit.log("Client reset", "Accepted")
	return {"ok": True}


def _client_settings():
	frappe.only_for("System Manager")
	settings = frappe.get_single(SETTINGS)
	if settings.mode != "Client":
		frappe.throw(_("This site is not in Client mode."))
	if not settings.site_url or not settings.server_url or not settings.public_key:
		frappe.throw(_("Save this site's URL and the server URL first."))
	settings.private = get_decrypted_password(SETTINGS, SETTINGS, "private_key")
	return settings


def _client_call(event, settings, method, data):
	try:
		reply = transport.post_signed(settings, method, data)
	except transport.NotAuthorised:
		return _client_failed(
			event,
			settings,
			_(
				"The server refused this site. If you have just connected, the server's admin still has to "
				"approve it. Otherwise check that the server's allowed list has this site exactly as {0}."
			).format(settings.site_url),
			status="Not authorised",
		)
	except transport.TransportError as e:
		return _client_failed(event, settings, str(e))
	if reply.get("for_site") != settings.site_url:
		return _client_failed(event, settings, _("The server's reply was meant for a different site."))

	status = CLIENT_STATUS.get(reply.get("status"), "Error")
	values = {"connection_status": status, "last_error": None}
	if status == "Connected":
		values["last_connected_on"] = now_datetime()
	frappe.db.set_single_value(SETTINGS, values)
	audit.log(event, "Accepted", site_url=settings.server_url, fingerprint=settings.server_fingerprint)
	return {
		"ok": True,
		"status": status,
		"own_fingerprint": settings.key_fingerprint,
		"server_fingerprint": settings.server_fingerprint,
	}


def _client_failed(event, settings, message, status="Error"):
	frappe.db.set_single_value(SETTINGS, {"connection_status": status, "last_error": message})
	audit.log(event, "Rejected", site_url=settings.server_url, reason=message)
	return {"ok": False, "status": status, "message": message}


# ── server ──────────────────────────────────────────────────────────────────


@frappe.whitelist(methods=["POST"])
def server_approve(rows):
	return _server_update(rows, "Approve")


@frappe.whitelist(methods=["POST"])
def server_revoke(rows):
	return _server_update(rows, "Revoke")


@frappe.whitelist(methods=["POST"])
def server_reset(rows):
	return _server_update(rows, "Reset")


def _server_update(rows, action):
	frappe.only_for("System Manager")
	if frappe.db.get_single_value(SETTINGS, "mode") != "Server":
		frappe.throw(_("This site is not in Server mode."))
	names = frappe.parse_json(rows) if isinstance(rows, str) else rows
	if not isinstance(names, list) or not names:
		frappe.throw(_("Select one or more sites first."))

	done = []
	for name in names:
		row = frappe.db.get_value(
			ALLOWED_SITE,
			{"name": name, "parent": SETTINGS, "parenttype": SETTINGS},
			["name", "site_url", "enabled", "status", "public_key", "fingerprint"],
			as_dict=True,
		)
		if not row:
			frappe.throw(_("A selected site was not found. Save the settings and try again."))
		if action == "Approve":
			if not row.public_key:
				frappe.throw(_("{0} has not sent a connect request yet.").format(row.site_url))
			if row.status != "Pending":
				frappe.throw(
					_("{0} is {1}. Only a waiting site can be approved.").format(row.site_url, row.status)
				)
			if not row.enabled:
				frappe.throw(_("{0} is disabled. Enable it before approving.").format(row.site_url))
			values = {"status": "Active", "approved_on": now_datetime(), "approved_by": frappe.session.user}
		elif action == "Revoke":
			values = {"status": "Revoked"}
		else:
			values = RESET_ROW
		frappe.db.set_value(ALLOWED_SITE, row.name, values, update_modified=False)
		audit.log(action, "Accepted", site_url=row.site_url, fingerprint=row.fingerprint)
		done.append(row.site_url)
	return {"ok": True, "sites": done}

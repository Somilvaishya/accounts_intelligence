"""Admin actions behind the settings form buttons. System Manager only."""

import frappe
from frappe import _
from frappe.utils import now_datetime
from frappe.utils.password import get_decrypted_password, remove_encrypted_password, set_encrypted_password

from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS, audit, keys, signing, transport

CLIENT_STATUS = {"Pending": "Waiting for approval", "Active": "Connected"}
RESET_ROW = {
	"status": "Pending",
	"public_key": None,
	"fingerprint": None,
	"requested_on": None,
	"approved_on": None,
	"approved_by": None,
	"last_seen": None,
	"key_rotated_on": None,
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


@frappe.whitelist(methods=["POST"])
def client_rotate_key():
	"""Replace this client's key while staying approved. Safe to click again after an interruption."""
	settings = _client_settings()
	if not settings.server_public_key:
		frappe.throw(_("Connect to the server first."))

	pending = _pending_key_pair()
	if pending:
		accepted = _server_accepts(settings, pending)
		if accepted is None:
			return _rotation_failed(
				settings,
				_(
					"An earlier rotation was interrupted and the server cannot be reached to check it. "
					"Try again when the server is reachable."
				),
			)
		if accepted:
			# The earlier rotation reached the server, but this site never recorded the switch.
			return _finish_rotation(settings, pending)

	new_private, new_public = keys.generate_keypair()
	_store_pending(new_private, new_public)
	nonce = signing.new_nonce()
	proof = keys.sign(
		new_private, signing.rotation_statement(settings.site_url, settings.public_key, new_public, nonce)
	)
	try:
		reply = transport.post_signed(
			settings, "rotate_key", {"new_public_key": new_public, "new_key_signature": proof}, nonce=nonce
		)
	except transport.NotAuthorised:
		_clear_pending()
		return _rotation_failed(
			settings,
			_("The server refused the new key. This site keeps its current key. See the server's Auth Log."),
		)
	except transport.TransportError as e:
		# The server may or may not have switched, so the new key is kept for the next attempt.
		return _rotation_failed(
			settings,
			_(
				"The rotation could not be confirmed ({0}). Click Rotate Key again: it completes the rotation "
				"if the server already switched, or starts a new one if not."
			).format(e),
		)
	if reply.get("for_site") != settings.site_url or reply.get("fingerprint") != keys.fingerprint(new_public):
		return _rotation_failed(settings, _("The server's reply did not confirm the new key."))
	return _finish_rotation(settings, (new_private, new_public))


@frappe.whitelist(methods=["POST"])
def regenerate_key():
	"""Replace this site's key pair outright, for when the private key may be known to someone else."""
	frappe.only_for("System Manager")
	settings = frappe.get_single(SETTINGS)
	if not settings.mode:
		frappe.throw(_("Choose and save this site's mode first."))
	private_key, public_key = keys.generate_keypair()
	fingerprint = _install_key(private_key, public_key)
	if settings.mode == "Client":
		message = _(
			"This site has a new key, so the server no longer accepts it. Ask the server's admin to reset "
			"this site's row, then click Connect to Server and have it approved again."
		)
		frappe.db.set_single_value(SETTINGS, {"connection_status": "Not connected", "last_error": message})
	else:
		message = _(
			"This server has a new key. Every client must click Reset Connection, check the new "
			"fingerprint, then click Connect to Server. Their approvals stay valid."
		)
	audit.log("Key regenerated", "Accepted", site_url=settings.site_url, fingerprint=fingerprint)
	return {"ok": True, "mode": settings.mode, "fingerprint": fingerprint, "message": message}


def _install_key(private_key, public_key) -> str:
	"""Make a key pair this site's identity and drop any half-finished rotation."""
	fingerprint = keys.fingerprint(public_key)
	set_encrypted_password(SETTINGS, SETTINGS, private_key, "private_key")
	frappe.db.set_single_value(
		SETTINGS,
		{
			"public_key": public_key,
			"key_fingerprint": fingerprint,
			"key_created_on": now_datetime(),
			"pending_public_key": None,
		},
	)
	remove_encrypted_password(SETTINGS, SETTINGS, "pending_private_key")
	return fingerprint


def _pending_key_pair():
	public_key = frappe.db.get_value(SETTINGS, SETTINGS, "pending_public_key")
	private_key = get_decrypted_password(SETTINGS, SETTINGS, "pending_private_key", raise_exception=False)
	return (private_key, public_key) if public_key and private_key else None


def _store_pending(private_key, public_key):
	set_encrypted_password(SETTINGS, SETTINGS, private_key, "pending_private_key")
	frappe.db.set_single_value(SETTINGS, "pending_public_key", public_key)
	# The server reads the new key from this site's identity endpoint while the rotation request is
	# still open, so it must be committed before the request is sent.
	_commit()


def _clear_pending():
	frappe.db.set_single_value(SETTINGS, "pending_public_key", None)
	remove_encrypted_password(SETTINGS, SETTINGS, "pending_private_key")


def _server_accepts(settings, key_pair):
	"""True if the server accepts a request signed with key_pair, False if it refuses, None if unknown."""
	try:
		transport.post_signed(settings, "ping", {}, key_pair=key_pair)
		return True
	except transport.NotAuthorised:
		return False
	except transport.TransportError:
		return None


def _finish_rotation(settings, key_pair):
	fingerprint = _install_key(*key_pair)
	frappe.db.set_single_value(
		SETTINGS, {"connection_status": "Connected", "last_connected_on": now_datetime(), "last_error": None}
	)
	audit.log("Key rotated", "Accepted", site_url=settings.server_url, fingerprint=fingerprint)
	return {
		"ok": True,
		"status": "Connected",
		"own_fingerprint": fingerprint,
		"server_fingerprint": settings.server_fingerprint,
	}


def _rotation_failed(settings, message):
	audit.log("Rotate key", "Rejected", site_url=settings.server_url, reason=message)
	return {"ok": False, "message": message}


def _commit():
	if not frappe.flags.in_test:
		frappe.db.commit()


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
def server_approve(rows, fingerprints=None):
	"""Approve waiting sites. `fingerprints` maps each row to the fingerprint the admin compared, so a key
	that changed after the admin looked is never approved."""
	return _server_update(rows, "Approve", fingerprints)


@frappe.whitelist(methods=["POST"])
def server_revoke(rows):
	return _server_update(rows, "Revoke")


@frappe.whitelist(methods=["POST"])
def server_reset(rows):
	return _server_update(rows, "Reset")


def _server_update(rows, action, fingerprints=None):
	frappe.only_for("System Manager")
	if frappe.db.get_single_value(SETTINGS, "mode") != "Server":
		frappe.throw(_("This site is not in Server mode."))
	names = frappe.parse_json(rows) if isinstance(rows, str) else rows
	if not isinstance(names, list) or not names:
		frappe.throw(_("Select one or more sites first."))
	seen = (frappe.parse_json(fingerprints) if isinstance(fingerprints, str) else fingerprints) or {}
	if not isinstance(seen, dict):
		frappe.throw(_("Reload the settings and try again."))

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
			if seen.get(row.name) != row.fingerprint:
				frappe.throw(
					_(
						"{0} sent a different key after this page was loaded. Reload, compare the new "
						"fingerprint with that site again, then approve."
					).format(row.site_url)
				)
			values = {"status": "Active", "approved_on": now_datetime(), "approved_by": frappe.session.user}
		elif action == "Revoke":
			values = {"status": "Revoked"}
		else:
			values = RESET_ROW
		frappe.db.set_value(ALLOWED_SITE, row.name, values, update_modified=False)
		audit.log(action, "Accepted", site_url=row.site_url, fingerprint=row.fingerprint)
		done.append(row.site_url)
	return {"ok": True, "sites": done}

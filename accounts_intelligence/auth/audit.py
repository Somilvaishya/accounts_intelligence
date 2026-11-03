import frappe

from accounts_intelligence.auth import AUTH_LOG


def log(event, result, site_url="", reason="", fingerprint=""):
	"""Record an authentication event. Rejection reasons live here and are never sent to the caller."""
	user = getattr(getattr(frappe.local, "session", None), "user", None)
	frappe.get_doc(
		{
			"doctype": AUTH_LOG,
			"event": event,
			"result": result,
			"site_url": (site_url or "")[:140],
			"fingerprint": fingerprint or "",
			"reason": (reason or "")[:1000],
			"ip_address": getattr(frappe.local, "request_ip", None) or "",
			"user": user if user and user != "Guest" else None,
		}
	).insert(ignore_permissions=True)

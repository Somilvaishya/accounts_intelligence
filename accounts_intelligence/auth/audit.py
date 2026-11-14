import frappe
from frappe.utils import add_days, now_datetime

from accounts_intelligence.auth import AUTH_LOG

# A burst of identical rejections writes a row only on these counts within one minute, so an
# unauthenticated caller cannot fill the table however many requests they send.
REJECTION_LOG_AT = (1, 10, 100, 1000, 10000)
REJECTION_WINDOW = 60

# Every site writes these on every run, so they are kept a week: enough for the Sync Health activity
# chart and recent troubleshooting. Everything else, refusals above all, stays for the 180 days in hooks.
ROUTINE_EVENTS = ("Sync", "Sync config")
ROUTINE_DAYS = 7


def clear_routine_entries():
	"""Delete accepted routine sync entries older than ROUTINE_DAYS. Runs daily."""
	frappe.db.delete(
		AUTH_LOG,
		{
			"event": ("in", ROUTINE_EVENTS),
			"result": "Accepted",
			"creation": ("<", add_days(now_datetime(), -ROUTINE_DAYS)),
		},
	)


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


def log_rejection(event, site_url, reason):
	"""Log a rejected request from another site, collapsing repeats of the same event and reason.

	The key ignores the claimed site URL and IP on purpose: both are chosen by the caller, so keying on
	them would let a flood create a new bucket for every request.
	"""
	key = frappe.cache.make_key(f"accounts_intelligence:rejected:{event}:{reason[:80]}")
	frappe.cache.set(key, 0, ex=REJECTION_WINDOW, nx=True)
	count = frappe.cache.incr(key)
	if count not in REJECTION_LOG_AT:
		return
	if count > 1:
		reason = f"{reason} (seen {count} times in the last minute)"
	log(event, "Rejected", site_url=site_url, reason=reason)

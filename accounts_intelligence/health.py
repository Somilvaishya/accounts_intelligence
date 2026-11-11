"""Sync health: how each company site is syncing, on one page.

Everything here is already on the server: the Allowed Site rows (status, last sync, daily check, and the
result the site reported after its last run), the synced documents, and the Auth Log, whose accepted Sync
entries show the site's activity hour by hour.
"""

import frappe
from frappe.utils import add_to_date, get_datetime, now_datetime

from accounts_intelligence.auth import ALLOWED_SITE, AUTH_LOG, SETTINGS
from accounts_intelligence.sync import DEFAULT_SYNC_EVERY, ENTITY, SYNC_EVERY, TRANSACTION

HOURS = 24


@frappe.whitelist(methods=["GET"])
def sites() -> dict:
	"""Every allowed site with its state, companies, last sync, daily check, warnings and activity."""
	frappe.only_for("System Manager")
	now = now_datetime()
	every = SYNC_EVERY.get(frappe.db.get_single_value(SETTINGS, "sync_every"), DEFAULT_SYNC_EVERY)
	rows = frappe.db.get_all(
		ALLOWED_SITE,
		{"parent": SETTINGS},
		[
			"site_url",
			"enabled",
			"status",
			"fingerprint",
			"approved_on",
			"last_seen",
			"key_rotated_on",
			"last_sync_on",
			"last_sync_rows",
			"sync_from",
			"resync_requested_on",
			"last_check",
			"last_result",
		],
		order_by="idx",
	)
	companies = {}
	for e in frappe.get_all(ENTITY, fields=["name", "company_name", "site_url"], order_by="company_name"):
		companies.setdefault(e.site_url, []).append({"pan": e.name, "name": e.company_name})
	documents = {
		r.entity: r
		for r in frappe.db.sql(
			f"""select entity, count(*) as documents, max(posting_date) as latest
			from `tab{TRANSACTION}` where is_cancelled = 0 group by entity""",
			as_dict=True,
		)
	}
	activity, rejected = _activity(now)

	result = []
	for row in rows:
		own = companies.get(row.site_url, [])
		result.append(
			{
				**row,
				"state": _state(row, now, every),
				"companies": own,
				"documents": sum(documents[c["pan"]].documents for c in own if c["pan"] in documents),
				"latest_document": max(
					(str(documents[c["pan"]].latest) for c in own if c["pan"] in documents), default=None
				),
				"warnings": warnings(row.last_result, row.last_check),
				"activity": activity.get(row.site_url, [0] * HOURS),
				"rejected": rejected.get(row.site_url, 0),
			}
		)
	return {"sync_every": every, "server_time": str(now), "sites": result}


def _state(row, now, every) -> str:
	if row.status != "Active":
		return row.status or "Pending"
	if not row.enabled:
		return "Disabled"
	if not row.last_sync_on:
		return "Never synced"
	if get_datetime(row.last_sync_on) < add_to_date(now, minutes=-2 * every):
		return "Late"
	return "Healthy"


def warnings(last_result, last_check) -> list[str]:
	"""What needs someone's attention: the notes in the site's last result, and a daily check that found
	months that differ."""
	found = []
	if last_result:
		head, _, notes = last_result.partition(" Notes: ")
		# A run that worked reports "Up to date." or "Sent ..."; anything else is why it did not.
		if not head.startswith(("Up to date", "Sent ")):
			found.append(f"Last run: {head}")
		found += [n.strip() for n in notes.split("; ") if n.strip()]
	if last_check and " differ" in last_check:
		found.append(f"Daily check: {last_check}")
	return found


def _activity(now) -> tuple[dict, dict]:
	"""Per site, accepted sync calls in each of the last 24 hours (oldest first), and rejected calls."""
	since = add_to_date(now, hours=-HOURS)
	activity, rejected = {}, {}
	for r in frappe.db.sql(
		f"""select site_url, result, floor(timestampdiff(minute, creation, %(now)s) / 60) as hours_ago,
			count(*) as calls
		from `tab{AUTH_LOG}`
		where event in ('Sync', 'Sync config') and creation >= %(since)s
		group by site_url, result, hours_ago""",
		{"now": now, "since": since},
		as_dict=True,
	):
		if r.result != "Accepted":
			rejected[r.site_url] = rejected.get(r.site_url, 0) + r.calls
			continue
		hours_ago = int(r.hours_ago)
		if 0 <= hours_ago < HOURS:
			activity.setdefault(r.site_url, [0] * HOURS)[HOURS - 1 - hours_ago] += r.calls
	return activity, rejected

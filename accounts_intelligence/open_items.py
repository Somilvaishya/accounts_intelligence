"""The open items work queue: every reconciliation line that does not match, across all company pairs,
and the review of each: who looks into it, and how it was explained.

The lines themselves are worked out by the reconciliation as always; only a review someone saves is stored,
in Accounts Intelligence Open Item, named by the line's key. A line that later matches simply stops being
open, and its review stays as a record.
"""

import frappe
from frappe import _
from frappe.utils import getdate, now_datetime

from accounts_intelligence import reconcile
from accounts_intelligence.reconcile import OPEN_ITEM
from accounts_intelligence.sync import ENTITY

STATUSES = ("Open", "In progress", "Explained")


@frappe.whitelist(methods=["GET"])
def items(from_date=None, to_date=None) -> dict:
	"""Every open line with its pair, age in days and review, and the people it can be assigned to."""
	frappe.only_for("System Manager")
	today = getdate()
	result = []
	for a, b in reconcile.pairs():
		data = reconcile.reconcile(a, b, from_date, to_date)
		for line in data["lines"]:
			if line["status"] != "matched":
				age = (today - getdate(line["date"])).days
				result.append({**line, "pair": {"a": data["a"], "b": data["b"]}, "age": age})
	result.sort(key=lambda line: -abs(line["difference"]))
	return {"items": result, "users": _users()}


@frappe.whitelist(methods=["POST"])
def review(a, b, key, status="Open", assigned_to=None, reason=None, note=None) -> dict:
	"""Save the review of one open line. The line is looked up again here, so only a line that really is
	open can be reviewed, and its description comes from the books, not from the request."""
	frappe.only_for("System Manager")
	if status not in STATUSES:
		frappe.throw(_("{0} is not a status.").format(status))
	if assigned_to and assigned_to not in {u["name"] for u in _users()}:
		frappe.throw(_("{0} cannot be assigned open items.").format(assigned_to))
	if not (frappe.db.exists(ENTITY, a) and frappe.db.exists(ENTITY, b)) or a == b:
		frappe.throw(_("Choose two different group companies."))

	first, second = sorted((a, b))
	data = reconcile.reconcile(first, second)
	line = next((x for x in data["lines"] if x["key"] == key and x["status"] != "matched"), None)
	if not line:
		frappe.throw(_("This line is no longer open. Reload the page."))

	exists = frappe.db.exists(OPEN_ITEM, key)
	doc = frappe.get_doc(OPEN_ITEM, key) if exists else frappe.new_doc(OPEN_ITEM)
	doc.update(
		{
			"status": status,
			"assigned_to": assigned_to or None,
			"reason": reason or None,
			"note": (note or "").strip()[:2000] or None,
			"pair_a": first,
			"pair_b": second,
			"kind": line["kind"],
			"line_date": line["date"],
			"documents": " ↔ ".join(d["name"] if d else "not recorded" for d in (line["a"], line["b"])),
			"difference": line["difference"],
			"reviewed_by": frappe.session.user,
			"reviewed_on": now_datetime(),
		}
	)
	if exists:
		doc.save(ignore_permissions=True)
	else:
		doc.insert(ignore_permissions=True, set_name=key)
	return {k: doc.get(k) for k in ("status", "assigned_to", "reason", "note")}


def _users() -> list[dict]:
	"""Enabled users who can open Accounts Intelligence: System Managers."""
	names = frappe.get_all("Has Role", {"role": "System Manager", "parenttype": "User"}, pluck="parent")
	return frappe.get_all(
		"User",
		{"name": ("in", names), "enabled": 1, "user_type": "System User"},
		["name", "full_name"],
		order_by="full_name",
	)

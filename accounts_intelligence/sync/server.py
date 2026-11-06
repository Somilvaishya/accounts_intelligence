"""Server side of data sync.

Both handlers run inside auth.server.handle, so every call is signed by an approved client site. The site
comes from that signature, never from the body, and a site can only write rows for companies it registered.
Rows are written with one bulk upsert per batch: a resent batch is harmless, and an older copy of a
document never overwrites a newer one.
"""

import hashlib
import math
from datetime import date, datetime

import frappe
from frappe.utils import now_datetime

from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS, audit
from accounts_intelligence.auth.server import Rejected
from accounts_intelligence.sync import (
	BALANCE,
	DEFAULT_SYNC_EVERY,
	ENTITY,
	MAX_ROWS,
	PARTY_TYPES,
	SOURCES,
	SYNC_EVERY,
	TRANSACTION,
	iso_date,
	pan_of,
	row_key,
)

MAX_CHECK_KEYS = 20000
MAX_DIFFERENCES = 100
MAX_COMPANIES = 100
MAX_SKIPPED = 20
PAYMENT_TYPES = ("", "Pay", "Receive")


def config_handler(request) -> dict:
	"""Register the client's companies and return the watch list: the PAN of every group company."""
	site = request.row.site_url
	companies = request.data.get("companies")
	if not isinstance(companies, list) or len(companies) > MAX_COMPANIES:
		raise Rejected("company list is not valid")

	known = {e.name: e for e in frappe.get_all(ENTITY, fields=["name", "company_name", "site_url"])}
	accepted, refused = [], []
	for company in companies:
		name = _text(company.get("company")) if isinstance(company, dict) else ""
		pan = pan_of(company.get("pan")) if isinstance(company, dict) else None
		if not name or not pan:
			refused.append({"company": name, "reason": "no valid PAN"})
			continue
		entity = known.get(pan)
		if entity and entity.site_url != site:
			refused.append({"company": name, "reason": "this PAN is registered to another site"})
			continue
		if not entity:
			frappe.get_doc({"doctype": ENTITY, "pan": pan, "company_name": name, "site_url": site}).insert(
				ignore_permissions=True
			)
			known[pan] = frappe._dict(name=pan, company_name=name, site_url=site)
		elif entity.company_name != name:
			frappe.db.set_value(ENTITY, pan, "company_name", name)
		accepted.append(pan)

	if refused:
		reasons = "; ".join(f"{r['company'] or '?'}: {r['reason']}" for r in refused)
		audit.log("Sync config", "Rejected", site_url=site, reason=f"companies refused: {reasons}")
	watch = sorted(known)
	rules = site_rules(request.row.name)
	# Every client run starts here, even when it has nothing to send, so this is when the site last synced.
	frappe.db.set_value(ALLOWED_SITE, request.row.name, "last_sync_on", now_datetime(), update_modified=False)
	return {
		"ok": True,
		"entities": accepted,
		"refused": refused,
		"watch": watch,
		# Names only let a client warn about a party named like a group company without a matching PAN.
		"names": {pan: known[pan].company_name for pan in watch},
		"version": hashlib.sha256(",".join(watch).encode()).hexdigest()[:16],
		**rules,
	}


def site_rules(row_name) -> dict:
	"""The rules one client follows: the site's own start date if set, else the group's, the group's
	interval, and the time of the last full resync the admin asked for."""
	group = frappe.db.get_value(SETTINGS, SETTINGS, ["sync_from", "sync_every"], as_dict=True) or {}
	row = (
		frappe.db.get_value(ALLOWED_SITE, row_name, ["sync_from", "resync_requested_on"], as_dict=True) or {}
	)
	return {
		"sync_from": iso_date(row.get("sync_from")) or iso_date(group.get("sync_from")),
		"sync_every": SYNC_EVERY.get(group.get("sync_every"), DEFAULT_SYNC_EVERY),
		"resync": str(row["resync_requested_on"]) if row.get("resync_requested_on") else None,
	}


def sync_handler(request) -> dict:
	"""Store a batch of document headers and party balances from the client, and compare the client's
	monthly totals with the stored ones when it sends its daily check."""
	site = request.row.site_url
	transactions = _batch(request.data.get("transactions"))
	balances = _batch(request.data.get("balances"))
	own = set(frappe.get_all(ENTITY, {"site_url": site}, pluck="name"))
	group = set(frappe.get_all(ENTITY, pluck="name"))

	skipped = []
	transaction_rows = _clean(transactions, _transaction, own, group, skipped)
	balance_rows = _clean(balances, _balance, own, group, skipped)
	_upsert(TRANSACTION, transaction_rows, version="source_modified")
	_upsert(BALANCE, balance_rows)

	values = {"last_sync_on": now_datetime()}
	if transaction_rows or balance_rows:
		# The daily check arrives without rows; it must not hide the size of the last real batch.
		values["last_sync_rows"] = len(transaction_rows) + len(balance_rows)
	reply = {
		"ok": True,
		"transactions": len(transaction_rows),
		"balances": len(balance_rows),
		"skipped": skipped[:MAX_SKIPPED],
		"skipped_count": len(skipped),
	}
	if "check" in request.data:
		reply["differences"], values["last_check"] = _check(
			request.data["check"], own, site_rules(request.row.name)["sync_from"]
		)
	frappe.db.set_value(ALLOWED_SITE, request.row.name, values, update_modified=False)
	return reply


def _check(totals, own, sync_from) -> tuple[list, str]:
	"""Compare the client's count and total per document type, company and month with what is stored.

	Keys are "doctype|PAN|YYYY-MM" and values [count, total in company currency]."""
	if not isinstance(totals, dict) or len(totals) > MAX_CHECK_KEYS:
		raise Rejected("check is not valid")
	stored = {}
	if own:
		for row in frappe.db.sql(
			f"""select source_doctype, entity, date_format(posting_date, '%%Y-%%m') as month,
				count(*) as count, sum(base_grand_total) as total
			from `tab{TRANSACTION}`
			where entity in %(own)s and posting_date >= %(sync_from)s
			group by source_doctype, entity, month""",
			{"own": tuple(own), "sync_from": sync_from or "1900-01-01"},
			as_dict=True,
		):
			stored[f"{row.source_doctype}|{row.entity}|{row.month}"] = [row.count, round(row.total, 2)]

	differences = []
	for key in sorted(set(stored) | set(totals)):
		try:
			count, total = int(totals.get(key, [0, 0])[0]), round(_amount(totals.get(key, [0, 0])[1]), 2)
		except (IndexError, TypeError, ValueError):
			raise Rejected("check is not valid")
		server = stored.get(key, [0, 0.0])
		if count != server[0] or abs(total - server[1]) > 0.01:
			differences.append({"key": str(key)[:140], "client": [count, total], "server": server})

	checked = now_datetime().strftime("%Y-%m-%d %H:%M")
	if not differences:
		return [], f"{checked}: all {len(stored)} months match"
	shown = "; ".join(
		f"{d['key']} client {d['client'][0]} / {d['client'][1]}, server {d['server'][0]} / {d['server'][1]}"
		for d in differences[:5]
	)
	return differences[:MAX_DIFFERENCES], f"{checked}: {len(differences)} differ. {shown}"[:1000]


def _batch(rows) -> list:
	if rows is None:
		return []
	if not isinstance(rows, list):
		raise Rejected("batch is not a list")
	if len(rows) > MAX_ROWS:
		raise Rejected("batch has too many rows")
	return rows


def _clean(rows, cleaner, own, group, skipped) -> list:
	"""Validate each row. A bad row is skipped and reported back; it never fails the whole batch."""
	result = {}
	for row in rows:
		try:
			if not isinstance(row, dict):
				raise ValueError("row is not an object")
			values = cleaner(row)
			if values["entity"] not in own:
				raise ValueError("company is not registered to this site")
			if values["counterparty"] not in group:
				raise ValueError("counterparty is not a group company")
			if values["counterparty"] == values["entity"]:
				raise ValueError("a company cannot be its own counterparty")
		except (KeyError, TypeError, ValueError) as e:
			skipped.append({"row": _label(row), "reason": str(e)[:140]})
			continue
		result[values["name"]] = values
	return list(result.values())


def _label(row) -> str:
	if not isinstance(row, dict):
		return ""
	return f"{row.get('source_doctype') or ''} {row.get('source_name') or row.get('party') or ''}".strip()[
		:140
	]


def _transaction(row) -> dict:
	source_doctype = _choice(row["source_doctype"], SOURCES)
	party_type = _choice(row["party_type"], PARTY_TYPES)
	values = {
		"entity": _text(row["entity"], required=True),
		"counterparty": _text(row["counterparty"], required=True),
		"source_doctype": source_doctype,
		"source_name": _text(row["source_name"], required=True),
		"posting_date": date.fromisoformat(row["posting_date"]),
		"party_type": party_type,
		"party": _text(row["party"], required=True),
		"party_name": _text(row.get("party_name")),
		"is_return": 1 if row.get("is_return") else 0,
		"is_cancelled": 1 if row.get("is_cancelled") else 0,
		"bill_no": _text(row.get("bill_no")),
		"payment_type": _choice(row.get("payment_type") or "", PAYMENT_TYPES),
		"reference_no": _text(row.get("reference_no")),
		"currency": _text(row.get("currency"))[:10],
		"grand_total": _amount(row.get("grand_total")),
		"net_total": _amount(row.get("net_total")),
		"tax_amount": _amount(row.get("tax_amount")),
		"base_grand_total": _amount(row.get("base_grand_total")),
		"source_modified": datetime.fromisoformat(row["source_modified"]),
	}
	# A journal entry gives one row per party, so the party is part of the key.
	values["name"] = row_key(
		values["entity"], source_doctype, values["source_name"], party_type, values["party"]
	)
	return values


def _balance(row) -> dict:
	values = {
		"entity": _text(row["entity"], required=True),
		"counterparty": _text(row["counterparty"], required=True),
		"party_type": _choice(row["party_type"], PARTY_TYPES),
		"party": _text(row["party"], required=True),
		"as_of": date.fromisoformat(row["as_of"]),
		"balance": _amount(row["balance"]),
	}
	values["name"] = row_key(
		values["entity"], values["party_type"], values["party"], values["as_of"].isoformat()
	)
	return values


def _text(value, required=False) -> str:
	value = "" if value is None else str(value).strip()[:140]
	if required and not value:
		raise ValueError("a required value is missing")
	return value


def _choice(value, options) -> str:
	if value not in options:
		raise ValueError(f"{value!r} is not allowed")
	return value


def _amount(value) -> float:
	if value is None or value == "":
		return 0.0
	number = float(value)
	if not math.isfinite(number):
		raise ValueError("amount is not a number")
	return number


def _upsert(doctype, rows, version=None):
	"""Insert or update rows by name in one statement. With `version`, a stored row is only replaced by a
	row whose version is the same or newer."""
	if not rows:
		return
	fields = [f for f in rows[0] if f != "name"]
	columns = ["name", "creation", "modified", "modified_by", "owner", *fields]
	now = now_datetime()
	values = []
	for row in rows:
		values.extend([row["name"], now, now, "Administrator", "Administrator", *(row[f] for f in fields)])

	def assign(field):
		new = f"VALUES(`{field}`)"
		return (
			f"`{field}` = IF(VALUES(`{version}`) >= `{version}`, {new}, `{field}`)"
			if version
			else f"`{field}` = {new}"
		)

	# The version column is assigned last: MariaDB applies assignments in order, and the others compare
	# against the stored version.
	updates = [assign(f) for f in [*fields, "modified"] if f != version]
	if version:
		updates.append(f"`{version}` = GREATEST(`{version}`, VALUES(`{version}`))")
	row_placeholder = "(" + ", ".join(["%s"] * len(columns)) + ")"
	frappe.db.sql(
		f"""INSERT INTO `tab{doctype}` ({", ".join(f"`{c}`" for c in columns)})
		VALUES {", ".join([row_placeholder] * len(rows))}
		ON DUPLICATE KEY UPDATE {", ".join(updates)}""",
		values,
	)

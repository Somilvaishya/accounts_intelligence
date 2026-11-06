"""Client side of data sync: send the headers of documents with other group companies to the server.

A run fetches the watch list (every group company's PAN) and the sync rules, then, for each source doctype,
sends the documents changed since its watermark whose party is a group company, then the party balances
that changed. A watermark only moves after the server's signed reply accepts the batch, so a failed run
sends the same rows again next time, which the server's upsert makes harmless. Nothing is queued or
stored apart from the small state in Settings.
"""

import hashlib
import json
import re
import time

import frappe
from frappe.utils import add_to_date, flt, get_datetime, get_last_day, now_datetime, today
from frappe.utils.password import get_decrypted_password

from accounts_intelligence.auth import SETTINGS, transport
from accounts_intelligence.sync import (
	DEFAULT_SYNC_EVERY,
	MAX_ROWS,
	PARTY_TYPES,
	SOURCES,
	iso_date,
	pan_of,
)

# Scheduled jobs on the default queue time out at 300 seconds; a run stops sending well before that.
TIME_BUDGET = 120
JOURNAL_LIMIT = MAX_ROWS // 5
LOCK = "accounts_intelligence:sync:lock"
LOCK_TTL = 900
START = ["1900-01-01 00:00:00", ""]
PARTY_NAME = {"Customer": "customer_name", "Supplier": "supplier_name"}
TAX_ID_FIELDS = ("pan", "gstin", "tax_id")


def scheduled_sync():
	"""Called every 5 minutes. Sends only once the interval set on the server has passed."""
	if _due():
		run()


def _due() -> bool:
	values = frappe.db.get_value(SETTINGS, SETTINGS, ["last_sync_on", "sync_state"], as_dict=True)
	if not values or not values.last_sync_on:
		return True
	every = _load_state(values.sync_state).get("sync_every") or DEFAULT_SYNC_EVERY
	# One minute of slack, so a run is not pushed back a full 5 minutes when the scheduler fires early.
	return now_datetime() >= add_to_date(get_datetime(values.last_sync_on), minutes=every - 1)


def run(time_budget=TIME_BUDGET) -> str | None:
	"""Send what changed. Returns a summary, or None when sync is off or another run is in progress."""
	settings = _settings()
	if not settings:
		return None
	lock = frappe.cache.make_key(LOCK)
	if not frappe.cache.set(lock, 1, ex=LOCK_TTL, nx=True):
		return None
	try:
		return _run(settings, time.monotonic() + time_budget)
	finally:
		frappe.cache.delete(lock)


def _settings():
	settings = frappe.get_single(SETTINGS)
	if (
		settings.mode != "Client"
		or not settings.sync_enabled
		or settings.connection_status != "Connected"
		or not settings.server_public_key
	):
		return None
	settings.private = get_decrypted_password(SETTINGS, SETTINGS, "private_key")
	return settings


def _run(settings, deadline) -> str:
	state = _load_state(settings.sync_state)
	sent = {"transactions": 0, "balances": 0}
	try:
		# Fetched every run, so a company that joins the group is picked up on the next run.
		notes = _refresh_config(settings, state)
		companies = state.get("companies", {})
		# A party with this site's own PAN is the company itself under another GSTIN, not a group company.
		watch = set(state.get("watch", [])) - set(companies.values())
		parties = watched_parties(watch)
		notes += unmatched_names(state.get("names", {}), watch, parties)
		if companies and parties:
			for doctype in SOURCES:
				more = True
				while more and time.monotonic() < deadline:
					rows, watermark, more = collect(
						doctype, companies, parties, state["watermarks"].get(doctype), state.get("sync_from")
					)
					if not rows:
						break
					notes += _send(settings, transactions=rows)
					sent["transactions"] += len(rows)
					state["watermarks"][doctype] = watermark
					_save_state(state)
			if time.monotonic() < deadline:
				sent["balances"] = _send_balances(settings, state, companies, parties, notes)
			if time.monotonic() < deadline and state.get("checked_on") != today():
				notes += _daily_check(settings, state, companies, parties, sent)
		result = _summary(sent, notes, finished=time.monotonic() < deadline)
	except transport.TransportError as e:
		result = str(e)
	except Exception:
		# Batches already accepted stay recorded: the state is saved after each one.
		frappe.db.rollback()
		frappe.log_error(title="Accounts Intelligence: sync failed")
		state = _load_state(frappe.db.get_single_value(SETTINGS, "sync_state"))
		result = "Sync stopped on an unexpected error. See the Error Log."
	_save_state(state, result)
	return result


# ── watch list ──────────────────────────────────────────────────────────────


def _refresh_config(settings, state) -> list:
	companies, notes = own_companies()
	reply = transport.post_signed(
		settings, "sync_config", {"companies": [{"company": c, "pan": p} for c, p in companies.items()]}
	)
	_check_reply(settings, reply)
	accepted = set(reply.get("entities") or [])
	companies = {c: p for c, p in companies.items() if p in accepted}
	notes += [f"{r.get('company')}: {r.get('reason')}" for r in reply.get("refused") or []]
	# A new group company or a newly registered company of this site means older documents now qualify,
	# so everything is sent again. The server's upsert keeps that harmless.
	version = hashlib.sha256(f"{reply.get('version')}|{sorted(companies.items())}".encode()).hexdigest()[:16]
	sync_from = iso_date(reply.get("sync_from"))
	# A start date moved earlier makes older documents qualify too. Moved later, stored rows simply stay.
	earlier = state.get("sync_from") and (not sync_from or sync_from < state["sync_from"])
	# The server's admin asked for a full resync of this site.
	resync = reply.get("resync")
	if version != state.get("version") or earlier or (resync and resync != state.get("resync")):
		state.update(version=version, watermarks={}, balances={})
	state.update(
		watch=reply.get("watch") or [],
		names=reply.get("names") or {},
		companies=companies,
		sync_from=sync_from,
		sync_every=int(reply.get("sync_every") or DEFAULT_SYNC_EVERY),
		resync=resync,
	)
	return notes


def own_companies() -> tuple[dict, list]:
	"""The company this site represents, mapped to its PAN, and a note when it cannot be sent.

	A site represents one company: the Default Company in Global Defaults, or the only company there is.
	Other companies on the site, such as ERPNext's test companies, are never sent."""
	company = frappe.defaults.get_global_default("company")
	if not company:
		names = frappe.get_all("Company", pluck="name")
		if len(names) != 1:
			return {}, ["set the Default Company in Global Defaults: this site has several companies"]
		company = names[0]
	row = frappe.db.get_value("Company", company, _tax_fields("Company") or ["name"], as_dict=True) or {}
	pan = pan_of(*(row.get(f) for f in TAX_ID_FIELDS))
	if not pan:
		return {}, [f"{company}: no PAN or GSTIN, so it is not synced"]
	return {company: pan}, []


def watched_parties(watch: set) -> dict:
	"""Customers and suppliers that are group companies, as {(party_type, party): (pan, party_name)}.

	The PAN comes from the party's pan, gstin or tax_id, or else from the GSTIN on one of its addresses."""
	if not watch:
		return {}
	result = {}
	for party_type in PARTY_TYPES:
		fields = _tax_fields(party_type)
		if fields:
			conditions = " or ".join(
				f"upper(`{f}`) in %(watch)s or substr(upper(`{f}`), 3, 10) in %(watch)s" for f in fields
			)
			rows = frappe.db.sql(
				f"""select name, `{PARTY_NAME[party_type]}` as party_name, {", ".join(f"`{f}`" for f in fields)}
				from `tab{party_type}` where {conditions}""",
				{"watch": tuple(watch)},
				as_dict=True,
			)
			for row in rows:
				pan = pan_of(*(row.get(f) for f in TAX_ID_FIELDS))
				if pan in watch:
					result[(party_type, row.name)] = (pan, row.party_name or row.name)
	if frappe.get_meta("Address").has_field("gstin"):
		for row in frappe.db.sql(
			"""select l.link_doctype as party_type, l.link_name as party, a.gstin
			from `tabAddress` a
			join `tabDynamic Link` l on l.parent = a.name and l.parenttype = 'Address'
			where l.link_doctype in %(party_types)s and substr(upper(a.gstin), 3, 10) in %(watch)s""",
			{"party_types": PARTY_TYPES, "watch": tuple(watch)},
			as_dict=True,
		):
			key = (row.party_type, row.party)
			if key not in result:
				name = frappe.db.get_value(row.party_type, row.party, PARTY_NAME[row.party_type])
				result[key] = (pan_of(row.gstin), name or row.party)
	return result


SUFFIXES = re.compile(r"\b(m s|private|pvt|limited|ltd|llp|co|company|corporation)\b")


def _core_name(name) -> str:
	"""A company name without punctuation and legal suffixes: "M/S Garg Global Pvt. Ltd." -> "garg global"."""
	words = re.sub(r"[^a-z0-9]+", " ", str(name or "").lower())
	return " ".join(SUFFIXES.sub(" ", words).split())


def unmatched_names(names: dict, watch: set, parties: dict) -> list:
	"""Warn about customers and suppliers named like a group company whose PAN or GSTIN does not match.

	Names are never used to send documents, since they are not unique. This only shows what is missed."""
	found = set(parties)
	notes = []
	for pan, company in names.items():
		core = _core_name(company)
		if pan not in watch or not core:
			continue
		first = core.split()[0]
		for party_type in PARTY_TYPES:
			for row in frappe.get_all(
				party_type,
				filters={PARTY_NAME[party_type]: ["like", f"%{first}%"]},
				fields=["name", f"{PARTY_NAME[party_type]} as party_name"],
			):
				if (party_type, row.name) not in found and _core_name(row.party_name) == core:
					notes.append(
						f"{party_type} {row.name} is named like group company {company} but has no matching "
						"PAN or GSTIN, so its documents are not sent"
					)
	return notes


def _tax_fields(doctype) -> list:
	# pan and gstin come from India Compliance; tax_id is ERPNext's own field.
	meta = frappe.get_meta(doctype)
	return [f for f in TAX_ID_FIELDS if meta.has_field(f)]


# ── documents ───────────────────────────────────────────────────────────────


def collect(
	doctype, companies, parties, watermark=None, sync_from=None, until=None
) -> tuple[list, list | None, bool]:
	"""Rows changed after the watermark and posted between sync_from and until, the watermark of the
	last document included, and whether more documents may follow."""
	modified, name = watermark or START
	# A journal entry can expand to a row per party, so fewer are read at a time to stay under MAX_ROWS.
	limit = JOURNAL_LIMIT if doctype == "Journal Entry" else MAX_ROWS
	params = {
		**_filters(companies, parties, sync_from),
		"until": until or "9999-12-31",
		"modified": modified,
		"name": name,
		"limit": limit,
	}
	after = (
		"d.posting_date between %(sync_from)s and %(until)s"
		" and (d.modified > %(modified)s or (d.modified = %(modified)s and d.name > %(name)s))"
	)
	headers = frappe.db.sql(QUERIES[doctype].format(after=after), params, as_dict=True)
	if not headers:
		return [], None, False
	more = len(headers) == limit
	if doctype == "Journal Entry":
		headers = _journal_parties(headers, params)
	currency = dict(frappe.get_all("Company", fields=["name", "default_currency"], as_list=True))

	rows = []
	for d in headers:
		pan, party_name = parties[(d.party_type, d.party)]
		rows.append(
			{
				"entity": companies[d.company],
				"counterparty": pan,
				"source_doctype": doctype,
				"source_name": d.name,
				"posting_date": str(d.posting_date),
				"party_type": d.party_type,
				"party": d.party,
				"party_name": party_name,
				"is_return": d.get("is_return") or 0,
				"is_cancelled": 1 if d.docstatus == 2 else 0,
				"bill_no": d.get("bill_no") or "",
				"payment_type": d.get("payment_type") or "",
				"reference_no": d.get("reference_no") or "",
				"currency": d.get("currency") or currency.get(d.company) or "",
				"grand_total": flt(d.grand_total),
				"net_total": flt(d.get("net_total")),
				"tax_amount": flt(d.get("tax_amount")),
				"base_grand_total": flt(d.base_grand_total),
				"source_modified": str(d.modified),
			}
		)
	last = headers[-1]
	return rows, [str(last.modified), last.name], more


PARTY_FILTER = (
	"(({p}party_type = 'Customer' and {p}party in %(customers)s)"
	" or ({p}party_type = 'Supplier' and {p}party in %(suppliers)s))"
)
QUERIES = {
	"Sales Invoice": """
		select d.name, d.company, d.posting_date, 'Customer' as party_type, d.customer as party, d.is_return,
			d.currency, d.grand_total, d.net_total, d.total_taxes_and_charges as tax_amount,
			d.base_grand_total, d.docstatus, d.modified
		from `tabSales Invoice` d
		where d.docstatus in (1, 2) and d.company in %(companies)s and d.customer in %(customers)s and {after}
		order by d.modified, d.name limit %(limit)s""",
	"Purchase Invoice": """
		select d.name, d.company, d.posting_date, 'Supplier' as party_type, d.supplier as party, d.is_return,
			d.bill_no, d.currency, d.grand_total, d.net_total, d.total_taxes_and_charges as tax_amount,
			d.base_grand_total, d.docstatus, d.modified
		from `tabPurchase Invoice` d
		where d.docstatus in (1, 2) and d.company in %(companies)s and d.supplier in %(suppliers)s and {after}
		order by d.modified, d.name limit %(limit)s""",
	"Payment Entry": """
		select d.name, d.company, d.posting_date, d.party_type, d.party, d.payment_type, d.reference_no,
			if(d.payment_type = 'Receive', d.paid_to_account_currency, d.paid_from_account_currency) as currency,
			if(d.payment_type = 'Receive', d.received_amount, d.paid_amount) as grand_total,
			if(d.payment_type = 'Receive', d.base_received_amount, d.base_paid_amount) as base_grand_total,
			d.docstatus, d.modified
		from `tabPayment Entry` d
		where d.docstatus in (1, 2) and d.company in %(companies)s and d.payment_type in ('Pay', 'Receive')
			and """
	+ PARTY_FILTER.format(p="d.")
	+ """ and {after}
		order by d.modified, d.name limit %(limit)s""",
	# Journal entries are selected by header here; _journal_parties then gives one row per party.
	"Journal Entry": """
		select d.name, d.modified
		from `tabJournal Entry` d
		where d.docstatus in (1, 2) and d.company in %(companies)s and {after}
			and exists (select 1 from `tabJournal Entry Account` a
				where a.parent = d.name and a.parenttype = 'Journal Entry' and """
	+ PARTY_FILTER.format(p="a.")
	+ """)
		order by d.modified, d.name limit %(limit)s""",
}


def _journal_parties(headers, params) -> list:
	"""One row per journal entry and watched party, with the net amount in company currency.

	The returned rows keep the order of `headers`, so the last row still carries the watermark."""
	rows = frappe.db.sql(
		"""
		select d.name, d.company, d.posting_date, d.cheque_no as reference_no, d.docstatus, d.modified,
			a.party_type, a.party, sum(a.debit) - sum(a.credit) as grand_total,
			sum(a.debit) - sum(a.credit) as base_grand_total
		from `tabJournal Entry` d
		join `tabJournal Entry Account` a on a.parent = d.name and a.parenttype = 'Journal Entry'
		where d.name in %(names)s and """
		+ PARTY_FILTER.format(p="a.")
		+ """
		group by d.name, a.party_type, a.party
		order by d.modified, d.name, a.party_type, a.party""",
		{**params, "names": tuple(h.name for h in headers)},
		as_dict=True,
	)
	return rows


# ── balances ────────────────────────────────────────────────────────────────


def _send_balances(settings, state, companies, parties, notes) -> int:
	"""Send today's balance for every group-company party whose balance changed since it was last sent."""
	current = {}
	for row in party_balances(companies, parties):
		current[f"{row.company}\x1f{row.party_type}\x1f{row.party}"] = round(flt(row.balance), 2)
	previous = state.setdefault("balances", {})
	for key in previous:
		current.setdefault(key, 0.0)

	changed = []
	for key, balance in current.items():
		if previous.get(key) == balance:
			continue
		company, party_type, party = key.split("\x1f")
		if company not in companies or (party_type, party) not in parties:
			continue
		changed.append(
			{
				"key": key,
				"entity": companies[company],
				"counterparty": parties[(party_type, party)][0],
				"party_type": party_type,
				"party": party,
				"as_of": today(),
				"balance": balance,
			}
		)
	for start in range(0, len(changed), MAX_ROWS):
		batch = changed[start : start + MAX_ROWS]
		notes += _send(settings, balances=[{k: v for k, v in r.items() if k != "key"} for r in batch])
		for row in batch:
			previous[row["key"]] = row["balance"]
		_save_state(state)
	return len(changed)


def party_balances(companies, parties) -> list:
	return frappe.db.sql(
		"""
		select g.company, g.party_type, g.party, sum(g.debit) - sum(g.credit) as balance
		from `tabGL Entry` g
		where g.is_cancelled = 0 and g.company in %(companies)s and """
		+ PARTY_FILTER.format(p="g.")
		+ """
		group by g.company, g.party_type, g.party""",
		_filters(companies, parties),
		as_dict=True,
	)


def _filters(companies, parties, sync_from=None) -> dict:
	# An empty tuple is not valid SQL in "in (...)", so a value no party can have stands in for it.
	return {
		"companies": tuple(companies),
		"customers": tuple(p for t, p in parties if t == "Customer") or ("",),
		"suppliers": tuple(p for t, p in parties if t == "Supplier") or ("",),
		"sync_from": sync_from or "1900-01-01",
	}


# ── daily check ─────────────────────────────────────────────────────────────


def _daily_check(settings, state, companies, parties, sent) -> list:
	"""Once a day, compare count and total per document type and month with the server. Months the
	server is missing documents from are sent again; months where it holds more (documents deleted on
	this site) are only reported, on both sites."""
	totals = monthly_totals(companies, parties, state.get("sync_from"))
	reply = transport.post_signed(settings, "sync", {"check": totals})
	_check_reply(settings, reply)
	notes = []
	for difference in reply.get("differences") or []:
		doctype, _pan, month = [*str(difference.get("key")).split("|"), "", ""][:3]
		client, server = difference.get("client") or [0, 0], difference.get("server") or [0, 0]
		if doctype not in SOURCES or not re.fullmatch(r"\d{4}-\d{2}", month):
			continue
		if client[0] >= server[0]:
			resent = _resend_month(settings, doctype, companies, parties, month, state, notes)
			sent["transactions"] += resent
			notes.append(f"daily check sent {resent} {doctype} documents of {month} again")
		else:
			notes.append(f"{doctype} {month}: the server has {server[0]} documents, this site {client[0]}")
	state["checked_on"] = today()
	return notes


def _resend_month(settings, doctype, companies, parties, month, state, notes) -> int:
	start = max(f"{month}-01", state.get("sync_from") or "")
	until = str(get_last_day(f"{month}-01"))
	count, watermark, more = 0, None, True
	while more:
		rows, watermark, more = collect(doctype, companies, parties, watermark, start, until)
		if not rows:
			break
		notes += _send(settings, transactions=rows)
		count += len(rows)
	return count


def monthly_totals(companies, parties, sync_from=None) -> dict:
	"""{"doctype|PAN|YYYY-MM": [count, total in company currency]} for every document sync would send."""
	params = _filters(companies, parties, sync_from)
	totals = {}
	for doctype, query in TOTAL_QUERIES.items():
		for row in frappe.db.sql(query, params, as_dict=True):
			totals[f"{doctype}|{companies[row.company]}|{row.month}"] = [row.count, round(flt(row.total), 2)]
	return totals


MONTH = "date_format(d.posting_date, '%%Y-%%m')"
SUBMITTED = "d.docstatus in (1, 2) and d.company in %(companies)s and d.posting_date >= %(sync_from)s"
TOTAL_QUERIES = {
	"Sales Invoice": f"""
		select d.company, {MONTH} as month, count(*) as count, sum(d.base_grand_total) as total
		from `tabSales Invoice` d where {SUBMITTED} and d.customer in %(customers)s
		group by d.company, month""",
	"Purchase Invoice": f"""
		select d.company, {MONTH} as month, count(*) as count, sum(d.base_grand_total) as total
		from `tabPurchase Invoice` d where {SUBMITTED} and d.supplier in %(suppliers)s
		group by d.company, month""",
	"Payment Entry": f"""
		select d.company, {MONTH} as month, count(*) as count,
			sum(if(d.payment_type = 'Receive', d.base_received_amount, d.base_paid_amount)) as total
		from `tabPayment Entry` d where {SUBMITTED} and d.payment_type in ('Pay', 'Receive')
			and {PARTY_FILTER.format(p="d.")}
		group by d.company, month""",
	# One synced row per journal entry and party, so the inner query groups the same way.
	"Journal Entry": f"""
		select x.company, x.month, count(*) as count, sum(x.net) as total from (
			select d.company, {MONTH} as month, sum(a.debit) - sum(a.credit) as net
			from `tabJournal Entry` d
			join `tabJournal Entry Account` a on a.parent = d.name and a.parenttype = 'Journal Entry'
			where {SUBMITTED} and {PARTY_FILTER.format(p="a.")}
			group by d.name, a.party_type, a.party
		) x group by x.company, x.month""",
}


# ── sending and state ───────────────────────────────────────────────────────


def _send(settings, **batch) -> list:
	reply = transport.post_signed(settings, "sync", batch)
	_check_reply(settings, reply)
	return [f"{s.get('row')}: {s.get('reason')}" for s in reply.get("skipped") or []]


def _check_reply(settings, reply):
	if reply.get("for_site") != settings.site_url or not reply.get("ok"):
		raise transport.TransportError("The server's reply did not confirm the batch.")


def _load_state(raw) -> dict:
	try:
		state = json.loads(raw or "{}")
	except ValueError:
		state = {}
	if not isinstance(state, dict):
		state = {}
	state.setdefault("watermarks", {})
	return state


def _save_state(state, result=None):
	values = {"sync_state": json.dumps(state, separators=(",", ":"), default=str)}
	if result is not None:
		values.update(last_sync_on=now_datetime(), last_sync_result=result[:1000])
	frappe.db.set_single_value(SETTINGS, values)
	# Progress is kept batch by batch, so an interrupted run resumes instead of starting over.
	if not frappe.flags.in_test:
		frappe.db.commit()


def _summary(sent, notes, finished) -> str:
	if not sent["transactions"] and not sent["balances"]:
		text = "Up to date."
	else:
		text = f"Sent {sent['transactions']} documents and {sent['balances']} balances."
	if not finished:
		text += " More remain; the next run continues."
	if notes:
		text += " Notes: " + "; ".join(dict.fromkeys(notes))
	return text

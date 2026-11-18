"""Reconciliation: match what two group companies recorded about each other.

Worked out on request from the synced rows, so nothing is stored and nothing goes stale. For a pair of
companies A and B it matches:

- invoices: the seller's sales invoice with the buyer's purchase invoice, first by the buyer's bill number,
  then by the same date and amount;
- money: payments and journal entries, as the amount each one debits the other company's party account.
  A payment A makes is a receipt for B, so a matching pair adds up to zero;
- balances: A's balance with B and B's balance with A should also add up to zero.

Documents someone reconciled by hand on the pair page (MATCH) are taken out first, as one matched line per
match, and the rest is matched as above. Cancelled documents are left out.
"""

from datetime import date

import frappe
from frappe import _
from frappe.utils import add_to_date, fmt_money, getdate, now_datetime

from accounts_intelligence import roles
from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS
from accounts_intelligence.sync import (
	ACCOUNT_ENTRY,
	BALANCE,
	DEFAULT_SYNC_EVERY,
	ENTITY,
	SYNC_EVERY,
	TRANSACTION,
	row_key,
)

OPEN_ITEM = "Accounts Intelligence Open Item"
MATCH = "Accounts Intelligence Match"
MATCH_DOCUMENT = "Accounts Intelligence Match Document"
INVOICES = ("Sales Invoice", "Purchase Invoice")
# Documents one manual match may hold, both sides together.
MAX_MATCH_DOCUMENTS = 50

# Rupees. The two books round taxes differently, so the same invoice can differ by a few paise.
TOLERANCE = 1.0
# A bank transfer can be booked a few days apart on the two sides.
PAYMENT_DAYS = 3
STATUSES = ("matched", "amount_differs", "date_differs", "only_a", "only_b")
# How each status counts in the charts.
OUTCOME = {
	"matched": "matched",
	"amount_differs": "differs",
	"date_differs": "differs",
	"only_a": "missing",
	"only_b": "missing",
}
# Age of an open line in days: the upper limit of each bucket, the last one open-ended.
AGEING = ((30, "0-30 days"), (90, "31-90 days"), (180, "91-180 days"), (None, "Over 180 days"))


@frappe.whitelist(methods=["GET"])
def dashboard(from_date=None, to_date=None) -> dict:
	"""Every pair of group companies with data and how far their books agree, the group's lines per month
	and open lines by age, and how the company sites are syncing."""
	roles.check()
	result, months, ageing = (
		[],
		{},
		[{"bucket": label, "count": 0, "amount": 0.0} for _limit, label in AGEING],
	)
	for a, b in pairs():
		data = reconcile(a, b, from_date, to_date)
		result.append({"a": data["a"], "b": data["b"], "summary": data["summary"]})
		for month in data["trend"]["months"]:
			key = month["month"]
			total = months.setdefault(key, {"month": key, "matched": 0, "differs": 0, "missing": 0})
			for outcome in ("matched", "differs", "missing"):
				total[outcome] += month[outcome]
		for total, bucket in zip(ageing, data["trend"]["ageing"], strict=True):
			total["count"] += bucket["count"]
			total["amount"] = round(total["amount"] + bucket["amount"], 2)
	return {
		"pairs": result,
		"months": [months[key] for key in sorted(months)],
		"ageing": ageing,
		"sync": _sync_health(),
	}


def pairs() -> list[tuple[str, str]]:
	"""Every pair of group companies (PANs) with a synced document or balance, each pair once. A related
	party keeps no books here to compare with, so it is never half of a pair."""
	group = f"select name from `tab{ENTITY}`"
	rows = frappe.db.sql(
		f"""select distinct least(entity, counterparty), greatest(entity, counterparty)
		from `tab{TRANSACTION}` where entity != counterparty and counterparty in ({group})
		union select distinct least(entity, counterparty), greatest(entity, counterparty)
		from `tab{BALANCE}` where entity != counterparty and counterparty in ({group})
		order by 1, 2"""
	)
	return [(a, b) for a, b in rows]


def _sync_health() -> dict:
	"""Active company sites, those that have not synced for twice the interval, and the latest sync."""
	every = SYNC_EVERY.get(frappe.db.get_single_value(SETTINGS, "sync_every"), DEFAULT_SYNC_EVERY)
	sites = frappe.db.get_all(
		ALLOWED_SITE,
		{"parent": SETTINGS, "enabled": 1, "status": "Active"},
		["site_url", "last_sync_on"],
	)
	cutoff = add_to_date(now_datetime(), minutes=-2 * every)
	synced = [s.last_sync_on for s in sites if s.last_sync_on]
	return {
		"sites": len(sites),
		"late": [s.site_url for s in sites if s.last_sync_on and s.last_sync_on < cutoff],
		"last_sync_on": str(max(synced)) if synced else None,
		"sync_every": every,
	}


@frappe.whitelist(methods=["GET"])
def pair(a, b, from_date=None, to_date=None) -> dict:
	"""Every invoice and money line between companies a and b (PANs), matched or not."""
	roles.check()
	if not a or not b or a == b or not frappe.db.exists(ENTITY, a) or not frappe.db.exists(ENTITY, b):
		frappe.throw("Choose two different group companies.")
	return reconcile(a, b, from_date, to_date)


def reconcile(a, b, from_date=None, to_date=None) -> dict:
	matches = _matches(a, b)
	rows = _rows(a, b, from_date, to_date)
	if matches and (from_date or to_date):
		rows += _rows_outside(a, b, matches, rows)
	sides = {a: [], b: []}
	for row in rows:
		sides[row.entity].append(row)

	# What gets matched, by (company, document type, document): invoices as they are, payments, journals
	# and account entries per voucher. Manual matches take theirs first.
	invoices = {_unit_key(r): r for r in rows if r.source_doctype in INVOICES}
	money = {_unit_key(r): r for side in (a, b) for r in _money(sides[side])}
	lines, broken = _manual_lines(a, b, matches, invoices, money, period=bool(from_date or to_date))
	# A document outside the period was fetched only to complete a match, so it is not matched otherwise.
	invoices = {k: r for k, r in invoices.items() if not r.get("outside")}
	money = {k: r for k, r in money.items() if not r.get("outside")}

	for seller, buyer in ((a, b), (b, a)):
		sales = [
			r
			for (entity, doctype, _name), r in invoices.items()
			if entity == seller and doctype == "Sales Invoice"
		]
		purchases = [
			r
			for (entity, doctype, _name), r in invoices.items()
			if entity == buyer and doctype == "Purchase Invoice"
		]
		lines += _orient(match_invoices(sales, purchases), seller == a)
	lines += match_money(
		[r for (entity, *_rest), r in money.items() if entity == a],
		[r for (entity, *_rest), r in money.items() if entity == b],
	)
	lines.sort(key=lambda line: (line["date"], line["kind"], line["status"]))
	_attach_reviews(a, b, lines)

	balance = _balances(a, b)
	# The site URL lets the frontend link each document to the company site that holds it.
	entities = {
		e.name: e
		for e in frappe.get_all(ENTITY, {"name": ("in", (a, b))}, ["name", "company_name", "site_url"])
	}

	def company(pan):
		entity = entities.get(pan) or frappe._dict()
		return {"pan": pan, "name": entity.company_name or pan, "site_url": entity.site_url}

	return {
		"a": company(a),
		"b": company(b),
		"from_date": from_date,
		"to_date": to_date,
		"summary": _summary(lines, balance, whole_history=not from_date and not to_date),
		"balance": balance,
		"trend": _trend(lines),
		"lines": lines,
		"broken_matches": broken,
	}


def line_docs(line, side) -> list:
	"""A side's documents: all of a manual match's, otherwise the one the line has."""
	return line.get(f"{side}_docs") or ([line[side]] if line[side] else [])


def line_key(a, b, line) -> str:
	"""A stable name for a line, from its pair and its documents, so a review stays with the same line
	whichever way round the pair is asked for."""
	if line.get("match"):
		return row_key(*sorted((a, b)), line["kind"], "Match", line["match"]["name"])
	docs = [f"{d['doctype']}:{d['name']}" if d else "-" for d in (line["a"], line["b"])]
	if a > b:
		a, b, docs = b, a, docs[::-1]
	return row_key(a, b, line["kind"], *docs)


def _attach_reviews(a, b, lines):
	"""Give every line its key, and every open line the review someone saved for it, if any."""
	first, second = sorted((a, b))
	reviews = {
		r.name: r
		for r in frappe.get_all(
			OPEN_ITEM,
			{"pair_a": first, "pair_b": second},
			["name", "status", "assigned_to", "reason", "note"],
		)
	}
	for line in lines:
		line["key"] = line_key(a, b, line)
		review = reviews.get(line["key"]) if line["status"] != "matched" else None
		line["review"] = (
			{k: review[k] for k in ("status", "assigned_to", "reason", "note")} if review else None
		)


def _trend(lines) -> dict:
	"""For the charts: lines per month by outcome, and open lines by age with their total difference."""
	months = {}
	ageing = [{"bucket": label, "count": 0, "amount": 0.0} for _limit, label in AGEING]
	today = getdate()
	for line in lines:
		key = line["date"][:7]
		month = months.setdefault(key, {"month": key, "matched": 0, "differs": 0, "missing": 0})
		month[OUTCOME[line["status"]]] += 1
		if line["status"] != "matched":
			age = (today - getdate(line["date"])).days
			bucket = next(
				b for (limit, _label), b in zip(AGEING, ageing, strict=True) if limit is None or age <= limit
			)
			bucket["count"] += 1
			bucket["amount"] = round(bucket["amount"] + abs(line["difference"]), 2)
	return {"months": [months[key] for key in sorted(months)], "ageing": ageing}


ROW_FIELDS = """name, entity, source_doctype, source_name, posting_date, party_type, party, is_return,
	bill_no, payment_type, reference_no, base_grand_total as amount"""
PAIR_ROWS = """is_cancelled = 0 and ((entity = %(a)s and counterparty = %(b)s)
	or (entity = %(b)s and counterparty = %(a)s))"""


def _rows(a, b, from_date, to_date) -> list:
	return frappe.db.sql(
		f"""select {ROW_FIELDS} from `tab{TRANSACTION}`
		where {PAIR_ROWS} and posting_date between %(from_date)s and %(to_date)s
		order by posting_date, source_name, source_doctype, name""",
		{"a": a, "b": b, "from_date": from_date or "1900-01-01", "to_date": to_date or "2999-12-31"},
		as_dict=True,
	)


def _unit_key(row) -> tuple:
	return (row.entity, row.source_doctype, row.source_name)


def _rows_outside(a, b, matches, rows) -> list:
	"""The rows outside the period of the matches that have a document inside it, marked `outside`: a match
	shows whole whenever one of its documents is in the period."""
	inside = {_unit_key(r) for r in rows}
	wanted = set()
	for match in matches:
		keys = {_unit_key(d) for d in match.documents}
		if keys & inside:
			wanted |= keys - inside
	if not wanted:
		return []
	found = frappe.db.sql(
		f"""select {ROW_FIELDS} from `tab{TRANSACTION}`
		where {PAIR_ROWS} and source_name in %(names)s
		order by posting_date, source_name, source_doctype, name""",
		{"a": a, "b": b, "names": tuple({key[2] for key in wanted})},
		as_dict=True,
	)
	return [frappe._dict(r, outside=True) for r in found if _unit_key(r) in wanted]


# ── manual matches ──────────────────────────────────────────────────────────


def _matches(a, b) -> list:
	"""The pair's manual matches, oldest first, each with its documents (entity, source_doctype,
	source_name)."""
	first, second = sorted((a, b))
	matches = frappe.db.sql(
		f"""select name, kind, matched_by, matched_on, note from `tab{MATCH}`
		where pair_a = %s and pair_b = %s order by creation, name""",
		(first, second),
		as_dict=True,
	)
	if not matches:
		return []
	documents = {}
	for d in frappe.db.sql(
		f"""select parent, entity, source_doctype, source_name from `tab{MATCH_DOCUMENT}`
		where parenttype = %s and parent in %s order by idx""",
		(MATCH, tuple(m.name for m in matches)),
		as_dict=True,
	):
		documents.setdefault(d.parent, []).append(d)
	for match in matches:
		match.documents = documents.get(match.name, [])
	return matches


def _net(kind, items) -> float:
	"""What a set of documents adds to the balance difference, from (document type, amount) pairs. Invoices:
	the seller's sales less the buyer's purchases. Money: the amounts are already signed, so they add up."""
	if kind == "Invoice":
		return sum(amount if doctype == "Sales Invoice" else -amount for doctype, amount in items)
	return sum(amount for _doctype, amount in items)


def _match_info(match) -> dict:
	return {
		"name": match.name,
		"by": match.matched_by,
		"by_name": frappe.utils.get_fullname(match.matched_by) if match.matched_by else None,
		"on": str(match.matched_on) if match.matched_on else None,
		"note": match.note,
	}


def _manual_lines(a, b, matches, invoices, money, period=False) -> tuple[list, list]:
	"""One matched line per manual match, taking its documents out of `invoices` and `money`, and the matches
	that no longer hold.

	A match no longer holds when one of its documents was cancelled or is no longer synced, is in an older
	match too, or when an amount changed so the sides no longer agree. Its documents are then matched as if it
	were not there, and it is listed so someone can see why and undo it. With a period, a match none of whose
	documents is in it is simply out of view."""
	lines, broken = [], []
	for match in matches:
		pool = invoices if match.kind == "Invoice" else money
		keys = [_unit_key(d) for d in match.documents]
		units = [pool.get(key) for key in keys]
		if period and not any(u and not u.get("outside") for u in units):
			continue
		missing = [key[2] for key, unit in zip(keys, units, strict=True) if not unit]
		net = _net(match.kind, [(u.source_doctype, u.amount) for u in units if u])
		if len(set(keys)) != len(keys):
			reason = _("A document is listed twice.")
		elif missing:
			reason = _("{0}: cancelled, in another match, or no longer synced.").format(", ".join(missing))
		elif abs(net) > TOLERANCE:
			reason = _("The amounts no longer agree: the sides differ by {0}.").format(fmt_money(abs(net)))
		elif not keys or {u.entity for u in units} != {a, b}:
			reason = _("The documents are no longer from both companies.")
		else:
			reason = None
		if reason:
			broken.append(
				{
					**_match_info(match),
					"kind": match.kind,
					"reason": reason,
					"documents": [{"entity": key[0], "doctype": key[1], "name": key[2]} for key in keys],
				}
			)
			continue
		for key in keys:
			del pool[key]
		a_units = [u for u in units if u.entity == a]
		b_units = [u for u in units if u.entity == b]
		inside = [u for u in units if not u.get("outside")]
		lines.append(
			{
				"kind": match.kind,
				"status": "matched",
				"date": str(min(u.posting_date for u in inside)),
				"a": _doc(a_units[0]),
				"b": _doc(b_units[0]),
				"a_docs": [_doc(u) for u in a_units],
				"b_docs": [_doc(u) for u in b_units],
				"difference": round(net, 2),
				"match": _match_info(match),
			}
		)
	return lines, broken


@frappe.whitelist(methods=["POST"])
def match(a, b, documents, note=None) -> dict:
	"""Reconcile open documents of companies a and b by hand: entries the automatic matching leaves open,
	such as the same amount booked on different days. `documents` is a JSON list of {entity, doctype, name},
	from both companies, all invoices or all payments and journals, whose totals agree within TOLERANCE."""
	roles.check()
	if not a or not b or a == b or not frappe.db.exists(ENTITY, a) or not frappe.db.exists(ENTITY, b):
		frappe.throw(_("Choose two different group companies."))
	note = (note or "").strip()[:1000]
	if not note:
		frappe.throw(
			_("Write why these entries belong together. The note stays with the match for the audit.")
		)
	chosen = frappe.parse_json(documents) if isinstance(documents, str) else documents
	if not isinstance(chosen, list) or not chosen or not all(isinstance(d, dict) for d in chosen):
		frappe.throw(_("Choose the documents to reconcile."))
	if len(chosen) > MAX_MATCH_DOCUMENTS:
		frappe.throw(_("Reconcile at most {0} documents at a time.").format(MAX_MATCH_DOCUMENTS))
	keys = [(str(d.get("entity")), str(d.get("doctype")), str(d.get("name"))) for d in chosen]
	if len(set(keys)) != len(keys):
		frappe.throw(_("A document is chosen twice."))

	# One match at a time per pair: two people reconciling the same entries at once would otherwise both pass
	# the check below. The lock is held until the request commits.
	frappe.db.sql(f"select name from `tab{ENTITY}` where name in %s order by name for update", ((a, b),))
	# Only documents that are open now, over the whole history, can be matched; they are looked up again
	# here, so their amounts and dates come from the books and not from the request.
	data = reconcile(a, b)
	open_docs = {}
	for line in data["lines"]:
		if line["status"] != "matched":
			for entity, doc in ((a, line["a"]), (b, line["b"])):
				if doc:
					open_docs[(entity, doc["doctype"], doc["name"])] = (line["kind"], doc)
	found = []
	for key in keys:
		if key not in open_docs:
			frappe.throw(
				_("{0} is not an open document of this pair any more. Reload the page.").format(key[2])
			)
		found.append(open_docs[key])

	kinds = {line_kind for line_kind, _doc in found}
	if len(kinds) != 1:
		frappe.throw(_("Reconcile invoices with invoices, and payments and journals with each other."))
	kind = kinds.pop()
	if {key[0] for key in keys} != {a, b}:
		frappe.throw(_("Choose documents from both companies."))
	if kind == "Invoice":
		sellers = {key[0] for key in keys if key[1] == "Sales Invoice"}
		buyers = {key[0] for key in keys if key[1] == "Purchase Invoice"}
		if len(sellers) != 1 or len(buyers) != 1 or sellers == buyers:
			frappe.throw(_("Match one company's sales invoices with the other company's purchase invoices."))
	net = _net(kind, [(doc["doctype"], doc["amount"]) for _kind, doc in found])
	if abs(net) > TOLERANCE:
		frappe.throw(
			_(
				"The two sides differ by {0}. Only documents whose totals agree within {1} can be reconciled; "
				"explain the difference on Open Items instead."
			).format(fmt_money(abs(net)), fmt_money(TOLERANCE))
		)

	first, second = sorted((a, b))
	record = frappe.get_doc(
		{
			"doctype": MATCH,
			"pair_a": first,
			"pair_b": second,
			"kind": kind,
			"amount": round(
				abs(
					sum(
						doc["amount"]
						for key, (_kind, doc) in zip(keys, found, strict=True)
						if key[0] == first
					)
				),
				2,
			),
			"difference": round(net, 2),
			"matched_by": frappe.session.user,
			"matched_on": now_datetime(),
			"note": note,
			"documents": [
				{
					"entity": key[0],
					"source_doctype": key[1],
					"source_name": key[2],
					"posting_date": doc["date"],
					"amount": doc["amount"],
				}
				for key, (_kind, doc) in zip(keys, found, strict=True)
			],
		}
	)
	record.insert(ignore_permissions=True)
	return {"name": record.name}


@frappe.whitelist(methods=["POST"])
def unmatch(name) -> dict:
	"""Undo a manual match: its documents are matched automatically again, or open. Frappe keeps the
	deleted record under Deleted Documents."""
	roles.check()
	if not name or not frappe.db.exists(MATCH, name):
		frappe.throw(_("This match was already undone. Reload the page."))
	frappe.delete_doc(MATCH, name, ignore_permissions=True)
	return {"name": name}


# ── invoices ────────────────────────────────────────────────────────────────


def match_invoices(sales, purchases) -> list:
	"""Pair the seller's sales invoices with the buyer's purchase invoices.

	The buyer's bill number names the seller's invoice, in full or by its trailing number. A purchase
	invoice without a usable bill number is paired with the one sales invoice of the same date and amount.
	Returns lines with "seller" and "buyer" sides."""
	open_sales = {r.source_name.upper(): r for r in sales}
	pairs, left = [], []
	for purchase in purchases:
		sale = _by_bill_no(purchase, open_sales)
		if sale:
			pairs.append((sale, purchase))
			del open_sales[sale.source_name.upper()]
		else:
			left.append(purchase)

	unmatched = []
	for purchase in left:
		candidates = [
			s
			for s in open_sales.values()
			if s.posting_date == purchase.posting_date
			and s.is_return == purchase.is_return
			and abs(s.amount - purchase.amount) <= TOLERANCE
		]
		if len(candidates) == 1:
			pairs.append((candidates[0], purchase))
			del open_sales[candidates[0].source_name.upper()]
		else:
			unmatched.append(purchase)

	lines = [_line("Invoice", sale, purchase, sale.amount - purchase.amount) for sale, purchase in pairs]
	lines += [_line("Invoice", sale, None, sale.amount) for sale in open_sales.values()]
	lines += [_line("Invoice", None, purchase, -purchase.amount) for purchase in unmatched]
	return lines


def _by_bill_no(purchase, open_sales):
	bill = "".join((purchase.bill_no or "").split()).upper()
	if not bill:
		return None
	sale = open_sales.get(bill)
	if sale:
		return sale if sale.is_return == purchase.is_return else None
	if len(bill) < 3:
		return None
	# "0262" names SI090126-0262, but not SI090126-10262: the number must start after a separator.
	candidates = [
		s
		for name, s in open_sales.items()
		if name.endswith(bill)
		and not name[-len(bill) - 1 : -len(bill)].isalnum()
		and s.is_return == purchase.is_return
	]
	return candidates[0] if len(candidates) == 1 else None


def _orient(lines, seller_is_a) -> list:
	"""Turn seller/buyer lines into A/B lines."""
	for line in lines:
		seller, buyer = line.pop("seller"), line.pop("buyer")
		line["a"], line["b"] = (seller, buyer) if seller_is_a else (buyer, seller)
		if line["status"] == "only_seller":
			line["status"] = "only_a" if seller_is_a else "only_b"
		elif line["status"] == "only_buyer":
			line["status"] = "only_b" if seller_is_a else "only_a"
	return lines


def _line(kind, seller, buyer, difference) -> dict:
	if not buyer:
		status = "only_seller"
	elif not seller:
		status = "only_buyer"
	elif abs(difference) > TOLERANCE:
		status = "amount_differs"
	elif seller.posting_date != buyer.posting_date:
		status = "date_differs"
	else:
		status = "matched"
	first = seller or buyer
	return {
		"kind": kind,
		"status": status,
		"date": str(first.posting_date),
		"seller": _doc(seller),
		"buyer": _doc(buyer),
		"difference": round(difference, 2),
	}


# ── money ───────────────────────────────────────────────────────────────────


def _money(rows) -> list:
	"""Payments, journal entries and account entries as the amount each debits the other company's
	accounts.

	A journal entry has one row per party, and a voucher one row per related-party account; the rows of one
	voucher are added up. One that nets to zero only moves an amount between the company's own accounts for
	the same group company (customer, supplier, a loan account), so it is left out.
	"""
	money, journals = [], {}
	for row in rows:
		if row.source_doctype == "Payment Entry":
			money.append(frappe._dict(row, amount=row.amount if row.payment_type == "Pay" else -row.amount))
		elif row.source_doctype in ("Journal Entry", ACCOUNT_ENTRY):
			journal = journals.setdefault(row.source_name, frappe._dict(row, amount=0.0, party=[]))
			journal.amount += row.amount
			journal.party.append(row.party)
	for journal in journals.values():
		if abs(journal.amount) > TOLERANCE:
			journal.party = ", ".join(sorted(set(journal.party)))
			money.append(journal)
	return sorted(money, key=lambda r: (r.posting_date, r.source_name))


def match_money(a_rows, b_rows) -> list:
	"""Pair each of A's movements with one of B's that cancels it within PAYMENT_DAYS, nearest date first and
	a document of the same type before another."""
	open_b = list(b_rows)
	lines = []
	for row in a_rows:
		candidates = [
			r
			for r in open_b
			if abs(row.amount + r.amount) <= TOLERANCE
			and abs((getdate(row.posting_date) - getdate(r.posting_date)).days) <= PAYMENT_DAYS
		]
		if not candidates:
			lines.append(_money_line(row, None))
			continue
		match = min(
			candidates,
			key=lambda r: (
				abs((getdate(row.posting_date) - getdate(r.posting_date)).days),
				r.source_doctype != row.source_doctype,
			),
		)
		open_b.remove(match)
		lines.append(_money_line(row, match))
	lines += [_money_line(None, r) for r in open_b]
	return lines


def _money_line(a, b) -> dict:
	# Within PAYMENT_DAYS a different date is normal for a bank transfer, so the pair counts as matched.
	status = "matched" if a and b else "only_a" if a else "only_b"
	first = a or b
	return {
		"kind": "Money",
		"status": status,
		"date": str(first.posting_date),
		"a": _doc(a),
		"b": _doc(b),
		"difference": round((a.amount if a else 0) + (b.amount if b else 0), 2),
	}


def _doc(row) -> dict | None:
	if not row:
		return None
	doc = {
		"doctype": row.source_doctype,
		"name": row.source_name,
		"date": str(row.posting_date),
		"party": row.party,
		"amount": round(row.amount, 2),
	}
	for field in ("bill_no", "payment_type", "reference_no"):
		if row.get(field):
			doc[field] = row[field]
	return doc


# ── balances and summary ────────────────────────────────────────────────────


def _balances(a, b) -> dict:
	"""Each side's latest balance with the other. They should add up to zero."""
	rows = frappe.db.sql(
		f"""select x.entity, sum(x.balance) as balance, max(x.as_of) as as_of
		from `tab{BALANCE}` x
		join (select entity, party_type, party, max(as_of) as as_of from `tab{BALANCE}`
			where (entity = %(a)s and counterparty = %(b)s) or (entity = %(b)s and counterparty = %(a)s)
			group by entity, party_type, party) latest
			on latest.entity = x.entity and latest.party_type = x.party_type and latest.party = x.party
			and latest.as_of = x.as_of
		where (x.entity = %(a)s and x.counterparty = %(b)s) or (x.entity = %(b)s and x.counterparty = %(a)s)
		group by x.entity""",
		{"a": a, "b": b},
		as_dict=True,
	)
	by_entity = {r.entity: r for r in rows}
	a_balance = round(by_entity[a].balance, 2) if a in by_entity else None
	b_balance = round(by_entity[b].balance, 2) if b in by_entity else None
	as_of = max((r.as_of for r in rows), default=None)
	return {
		"a": a_balance,
		"b": b_balance,
		"difference": round((a_balance or 0) + (b_balance or 0), 2),
		"as_of": str(as_of) if isinstance(as_of, date) else as_of,
	}


def _summary(lines, balance, whole_history) -> dict:
	"""Counts per kind and status. Every line's difference is what it adds to the two balances, so over the
	whole history the balance difference less the lines' differences is what no synced document explains:
	documents before the sync start date, or entries with a party that is not matched by PAN."""
	summary = {}
	for kind in ("Invoice", "Money"):
		counts = dict.fromkeys(STATUSES, 0)
		for line in lines:
			if line["kind"] == kind:
				counts[line["status"]] += 1
		summary[kind.lower()] = counts
	open_lines = [line for line in lines if line["status"] != "matched"]
	summary["open"] = len(open_lines)
	# Matched lines someone reconciled by hand on the pair page, which auditors ask about separately.
	summary["manual"] = sum(1 for line in lines if line.get("match"))
	# Open lines someone has looked into and explained; they still count as open, since the books differ.
	summary["explained"] = sum(
		1 for line in open_lines if (line["review"] or {}).get("status") == "Explained"
	)
	summary["balance_difference"] = balance["difference"]
	summary["unexplained"] = (
		round(balance["difference"] - sum(line["difference"] for line in lines), 2) if whole_history else None
	)
	return summary

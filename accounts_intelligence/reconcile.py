"""Reconciliation: match what two group companies recorded about each other.

Worked out on request from the synced rows, so nothing is stored and nothing goes stale. For a pair of
companies A and B it matches:

- invoices: the seller's sales invoice with the buyer's purchase invoice, first by the buyer's bill number,
  then by the same date and amount;
- money: payments and journal entries, as the amount each one debits the other company's party account.
  A payment A makes is a receipt for B, so a matching pair adds up to zero;
- balances: A's balance with B and B's balance with A should also add up to zero.

Cancelled documents are left out.
"""

from datetime import date

import frappe
from frappe.utils import add_to_date, getdate, now_datetime

from accounts_intelligence.auth import ALLOWED_SITE, SETTINGS
from accounts_intelligence.sync import BALANCE, DEFAULT_SYNC_EVERY, ENTITY, SYNC_EVERY, TRANSACTION

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
	frappe.only_for("System Manager")
	pairs = frappe.db.sql(
		f"""select distinct least(entity, counterparty) as a, greatest(entity, counterparty) as b
		from `tab{TRANSACTION}` where entity != counterparty
		union select distinct least(entity, counterparty), greatest(entity, counterparty)
		from `tab{BALANCE}` where entity != counterparty
		order by a, b""",
		as_dict=True,
	)
	result, months, ageing = [], {}, [{"bucket": label, "count": 0, "amount": 0.0} for _, label in AGEING]
	for pair_row in pairs:
		data = reconcile(pair_row.a, pair_row.b, from_date, to_date)
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
	frappe.only_for("System Manager")
	if not a or not b or a == b or not frappe.db.exists(ENTITY, a) or not frappe.db.exists(ENTITY, b):
		frappe.throw("Choose two different group companies.")
	return reconcile(a, b, from_date, to_date)


def reconcile(a, b, from_date=None, to_date=None) -> dict:
	rows = _rows(a, b, from_date, to_date)
	sides = {a: [], b: []}
	for row in rows:
		sides[row.entity].append(row)

	lines = []
	for seller, buyer in ((a, b), (b, a)):
		sales = [r for r in sides[seller] if r.source_doctype == "Sales Invoice"]
		purchases = [r for r in sides[buyer] if r.source_doctype == "Purchase Invoice"]
		lines += _orient(match_invoices(sales, purchases), seller == a)
	lines += match_money(_money(sides[a]), _money(sides[b]))
	lines.sort(key=lambda line: (line["date"], line["kind"], line["status"]))

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
	}


def _trend(lines) -> dict:
	"""For the charts: lines per month by outcome, and open lines by age with their total difference."""
	months = {}
	ageing = [{"bucket": label, "count": 0, "amount": 0.0} for _, label in AGEING]
	today = getdate()
	for line in lines:
		key = line["date"][:7]
		month = months.setdefault(key, {"month": key, "matched": 0, "differs": 0, "missing": 0})
		month[OUTCOME[line["status"]]] += 1
		if line["status"] != "matched":
			age = (today - getdate(line["date"])).days
			bucket = next(
				b for (limit, _), b in zip(AGEING, ageing, strict=True) if limit is None or age <= limit
			)
			bucket["count"] += 1
			bucket["amount"] = round(bucket["amount"] + abs(line["difference"]), 2)
	return {"months": [months[key] for key in sorted(months)], "ageing": ageing}


def _rows(a, b, from_date, to_date) -> list:
	return frappe.db.sql(
		f"""select entity, source_doctype, source_name, posting_date, party_type, party, is_return,
			bill_no, payment_type, reference_no, base_grand_total as amount
		from `tab{TRANSACTION}`
		where is_cancelled = 0 and ((entity = %(a)s and counterparty = %(b)s)
			or (entity = %(b)s and counterparty = %(a)s))
			and posting_date between %(from_date)s and %(to_date)s
		order by posting_date, source_name""",
		{"a": a, "b": b, "from_date": from_date or "1900-01-01", "to_date": to_date or "2999-12-31"},
		as_dict=True,
	)


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
	"""Payments and journal entries as the amount each debits the other company's party account.

	A journal entry has one row per party; its rows are added up, and one that nets to zero only moves an
	amount between the company's customer and supplier accounts of the same group company, so it is left out.
	"""
	money, journals = [], {}
	for row in rows:
		if row.source_doctype == "Payment Entry":
			money.append(frappe._dict(row, amount=row.amount if row.payment_type == "Pay" else -row.amount))
		elif row.source_doctype == "Journal Entry":
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
	summary["balance_difference"] = balance["difference"]
	summary["unexplained"] = (
		round(balance["difference"] - sum(line["difference"] for line in lines), 2) if whole_history else None
	)
	return summary

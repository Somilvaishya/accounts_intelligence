"""Internal transfers: goods one branch of a company sends to another of its branches, through Business Needed
Solutions (BNS), and whether the receiving branch booked them.

BNS books a transfer between branches with different GSTINs as a sales invoice to the company's internal
customer, answered by a purchase invoice from its internal supplier, and one between branches with the same
GSTIN as a delivery note answered by a purchase receipt. The two are linked by bns_inter_company_reference,
set on either document or both, and a purchase receipt can also name the delivery note on its items. A
sales invoice may also update stock itself, or bill a delivery note whose goods arrive on a purchase receipt
(or a purchase receipt may name the invoice); then the invoices carry the amount and the stock documents are
left out. The
sending branch debits BNS's Internal Debtor account and the receiving branch credits its Internal Creditor
account, so once every transfer is booked on both sides the two add up to zero.

The company's own site works this out (client side, `snapshot`) and sends the totals and the transfers that
need a look with each sync; the group server stores the latest list (`store`) and shows it (`overview`,
`transfers`). Nothing here reads another company's books: it is one company's branches against each other.

A transfer needs a look when:
- Not received: no submitted receiving document (one in draft is named in the note);
- Amount differs: what was received differs from what was sent by more than BNS's tolerance;
- Check party: the amounts agree, but a document is booked to a party that is not the company's internal
  customer or supplier;
- Not dispatched: a submitted receiving document with no submitted sending document.

Every transfer adds what was sent less what was received to the debtor and creditor balances, so the
balances' gap less those differences is what no transfer explains (unexplained).
"""

import hashlib
import json
from collections import defaultdict
from datetime import date

import frappe
from frappe.utils import add_to_date, flt, get_datetime, getdate, now_datetime, today

from accounts_intelligence import roles
from accounts_intelligence.sync import ENTITY, MAX_ROWS, plain, row_key

INTERNAL_TRANSFER = "Accounts Intelligence Internal Transfer"
BNS_SETTINGS = "BNS Branch Accounting Settings"
# The receiving document type for each sending one, and the kind of transfer.
RECEIVER = {"Sales Invoice": "Purchase Invoice", "Delivery Note": "Purchase Receipt"}
SENDER = {v: k for k, v in RECEIVER.items()}
KIND = {
	"Sales Invoice": "Invoice",
	"Delivery Note": "Delivery",
	"Purchase Invoice": "Invoice",
	"Purchase Receipt": "Delivery",
}
STATUSES = ("Not received", "Amount differs", "Check party", "Not dispatched")
# Receiving documents named on one listed transfer; the amounts always count all of them.
MAX_RECEIVERS = 20
# The check reads the company's whole history of transfers, so a site runs it at most this often.
CHECK_EVERY_MINUTES = 60
# The most a count in a summary can be, so a site cannot store absurd figures.
MAX_COUNT = 10**7
# Rupees, as BNS itself compares them: invoices within its setting (₹1 when unset), deliveries within ₹5.
INVOICE_TOLERANCE = 1.0
DELIVERY_TOLERANCE = 5.0
# The fields BNS adds. Without them the site does not use BNS and there is nothing to send.
BNS_FIELDS = (
	("Sales Invoice", "is_bns_internal_customer"),
	("Sales Invoice", "bns_inter_company_reference"),
	("Delivery Note", "is_bns_internal_customer"),
	("Delivery Note", "bns_inter_company_reference"),
	("Purchase Invoice", "is_bns_internal_supplier"),
	("Purchase Invoice", "bns_inter_company_reference"),
	("Purchase Receipt", "is_bns_internal_supplier"),
	("Purchase Receipt", "bns_inter_company_reference"),
)


# ── client: what this site's company has sent between its branches ─────────


def available() -> bool:
	return all(frappe.get_meta(doctype).has_field(field) for doctype, field in BNS_FIELDS)


def send(settings, state, companies, post) -> list:
	"""Check this site's internal transfers at most once every CHECK_EVERY_MINUTES, and send them when they
	changed, and at least once a day. `post` sends one batch and returns its notes. A site that never used
	BNS sends nothing."""
	if len(companies) != 1:
		return []
	checked = state.get("internal_checked")
	if checked and get_datetime(checked) > add_to_date(now_datetime(), minutes=-CHECK_EVERY_MINUTES):
		return []
	# Recorded before the check, so a check that fails is tried again an interval later, not every run.
	state["internal_checked"] = str(now_datetime())
	company, pan = next(iter(companies.items()))
	payload = {"entity": pan, "summary": None, "items": []}
	if available():
		found = snapshot(company)
		if found["summary"]["documents"] or found["items"] or found["summary"]["gap"]:
			payload.update(found)
	if payload["summary"] is None and not state.get("internal"):
		return []
	digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]
	if state.get("internal") == digest and state.get("internal_on") == today():
		return []
	notes = post(settings, internal=payload)
	state.update(internal=digest if payload["summary"] else None, internal_on=today())
	return notes


def snapshot(company) -> dict:
	"""The company's internal transfers that need a look, and its totals."""
	params = {"company": company}
	senders = []
	for doctype in RECEIVER:
		senders += frappe.db.sql(
			f"""select '{doctype}' as doctype, d.name, d.posting_date, d.base_grand_total as amount,
				d.customer as party, d.bns_inter_company_reference as ref,
				{_column(doctype, "company_gstin")} as from_gstin, {_column(doctype, "billing_address_gstin")} as to_gstin
			from `tab{doctype}` d
			where d.docstatus = 1 and d.company = %(company)s
				and (d.is_bns_internal_customer = 1 or d.status = 'BNS Internally Transferred')""",
			params,
			as_dict=True,
		)
	receivers = []
	for doctype in SENDER:
		# Between branches with different GSTINs, BNS can also receive the goods of a sales invoice on a
		# purchase receipt that names the invoice. Only the invoices carry the amount there, so such a receipt
		# is left out.
		stock_only = (
			" and not exists (select 1 from `tabSales Invoice` s where s.name = d.bns_inter_company_reference)"
			if doctype == "Purchase Receipt"
			else ""
		)
		receivers += frappe.db.sql(
			f"""select '{doctype}' as doctype, d.name, d.posting_date, d.docstatus, d.base_grand_total as amount,
				d.supplier as party, d.bns_inter_company_reference as ref,
				{_column(doctype, "supplier_gstin")} as from_gstin, {_column(doctype, "company_gstin")} as to_gstin
			from `tab{doctype}` d
			where d.docstatus in (0, 1) and d.company = %(company)s
				and (d.is_bns_internal_supplier = 1 or coalesce(d.bns_inter_company_reference, '') != ''){stock_only}""",
			params,
			as_dict=True,
		)
	links = defaultdict(list)
	if frappe.get_meta("Purchase Receipt Item").has_field("delivery_note_item"):
		for receiver, sender in frappe.db.sql(
			"""select distinct i.parent, n.parent
			from `tabPurchase Receipt Item` i
			join `tabDelivery Note Item` n on n.name = i.delivery_note_item
			join `tabPurchase Receipt` r on r.name = i.parent
			where r.company = %(company)s and r.docstatus in (0, 1) and coalesce(i.delivery_note_item, '') != ''""",
			params,
		):
			links[receiver].append(sender)
	billed = frappe.db.sql_list(
		"""select distinct i.delivery_note
		from `tabSales Invoice Item` i
		join `tabSales Invoice` s on s.name = i.parent
		where s.docstatus = 1 and s.company = %(company)s and coalesce(i.delivery_note, '') != ''
			and (s.is_bns_internal_customer = 1 or s.status = 'BNS Internally Transferred')""",
		params,
	)
	return assess(
		senders,
		receivers,
		links,
		internal=_internal_parties(company),
		tolerance=flt(_bns_setting("si_pi_amount_tolerance")) or INVOICE_TOLERANCE,
		balances=_balances(company),
		billed=set(billed),
	)


def _column(doctype, field) -> str:
	return f"d.`{field}`" if frappe.get_meta(doctype).has_field(field) else "''"


def _bns_setting(field):
	if not frappe.db.exists("DocType", BNS_SETTINGS) or not frappe.get_meta(BNS_SETTINGS).has_field(field):
		return None
	return frappe.db.get_single_value(BNS_SETTINGS, field)


def _internal_parties(company) -> set:
	"""The company's own internal customers and suppliers, as (party type, name)."""
	result = set()
	for party_type, flag in (
		("Customer", "is_bns_internal_customer"),
		("Supplier", "is_bns_internal_supplier"),
	):
		meta = frappe.get_meta(party_type)
		if not meta.has_field(flag):
			continue
		filters = {flag: 1}
		if meta.has_field("bns_represents_company"):
			filters["bns_represents_company"] = company
		result |= {(party_type, name) for name in frappe.get_all(party_type, filters, pluck="name")}
	return result


def _balances(company) -> dict:
	"""The balance of BNS's Internal Debtor and Internal Creditor accounts of this company."""
	result = {"debtor_account": None, "creditor_account": None, "debtor": None, "creditor": None}
	for side in ("debtor", "creditor"):
		account = _bns_setting(f"internal_branch_{side}_account")
		if not account or frappe.db.get_value("Account", account, "company") != company:
			continue
		result[f"{side}_account"] = account
		result[side] = flt(
			frappe.db.sql(
				"""select sum(debit) - sum(credit) from `tabGL Entry`
				where account = %s and company = %s and is_cancelled = 0""",
				(account, company),
			)[0][0],
			2,
		)
	return result


def assess(senders, receivers, links, internal, tolerance, balances, billed=frozenset()) -> dict:
	"""Pair each submitted sending document with its receiving documents and keep those that need a look.

	`senders` and `receivers` are rows with doctype, name, posting_date, amount, party, ref (the BNS link) and
	the GSTINs; receivers also carry docstatus. `links` maps a purchase receipt to the delivery notes its
	items name. `internal` holds the company's internal parties as (party type, name). `billed` holds the
	delivery notes an internal sales invoice bills: the invoice and its purchase invoice carry the amount,
	so the delivery note and the purchase receipt answering it only move the stock and are left out."""
	named_by = defaultdict(list)
	for s in senders:
		if s.get("ref"):
			named_by[(RECEIVER[s["doctype"]], s["ref"])].append(s["name"])
	by_key = {
		(s["doctype"], s["name"]): s
		for s in senders
		if not (s["doctype"] == "Delivery Note" and s["name"] in billed)
	}

	# Senders a receiving document answers together (one purchase receipt for two delivery notes) are
	# compared as one group, so neither shows a false difference.
	group = {key: key for key in by_key}

	def root(key):
		while group[key] != key:
			group[key] = key = group[group[key]]
		return key

	answered, orphans = [], []
	for r in receivers:
		source = SENDER[r["doctype"]]
		candidates = [r.get("ref"), *named_by.get((r["doctype"], r["name"]), []), *links.get(r["name"], [])]
		if source == "Delivery Note" and any(n in billed for n in candidates):
			continue
		keys = list(dict.fromkeys((source, n) for n in candidates if n and (source, n) in by_key))
		if keys:
			for other in keys[1:]:
				group[root(other)] = root(keys[0])
			answered.append((keys[0], r))
		elif r["docstatus"] == 1:
			orphans.append(r)

	groups = defaultdict(lambda: {"senders": [], "received": [], "drafts": []})
	for key in by_key:
		groups[root(key)]["senders"].append(by_key[key])
	for key, r in answered:
		groups[root(key)]["received" if r["docstatus"] == 1 else "drafts"].append(r)

	items, explained = [], 0.0
	totals = {status: {"count": 0, "amount": 0.0} for status in STATUSES}
	for g in groups.values():
		g["senders"].sort(key=lambda s: (str(s["posting_date"]), s["name"]))
		first, got = g["senders"][0], g["received"]
		sent = flt(sum(flt(s["amount"]) for s in g["senders"]), 2)
		booked = flt(sum(flt(r["amount"]) for r in got), 2)
		difference = flt(sent - booked, 2)
		explained += difference
		limit = tolerance if first["doctype"] == "Sales Invoice" else DELIVERY_TOLERANCE
		party = [
			f"customer {s['party']} is not an internal customer of this company"
			for s in g["senders"]
			if ("Customer", s["party"]) not in internal
		] + [
			f"{r['name']} is booked to supplier {r['party']}, not an internal supplier"
			for r in got
			if ("Supplier", r["party"]) not in internal
		]
		info = []
		if len(g["senders"]) > 1:
			info.append("received together with " + ", ".join(s["name"] for s in g["senders"][1:]))
		if len(got) > 1:
			info.append(f"received in {len(got)} documents")
		if not got:
			status = "Not received"
			info += [f"{r['doctype']} {r['name']} is in draft" for r in g["drafts"]]
		elif abs(difference) > limit:
			status = "Amount differs"
		elif party:
			status = "Check party"
		else:
			continue
		totals[status]["count"] += 1
		totals[status]["amount"] += abs(difference)
		items.append(_item(first, status, sent, booked, difference, got, party + info))

	for r in orphans:
		amount = flt(r["amount"], 2)
		explained -= amount
		note = (
			f"names {r['ref']}, which is not a submitted internal {SENDER[r['doctype']].lower()}"
			if r.get("ref")
			else "names no sending document"
		)
		totals["Not dispatched"]["count"] += 1
		totals["Not dispatched"]["amount"] += abs(amount)
		items.append(_item(r, "Not dispatched", 0.0, amount, -amount, [], [note]))

	gap = None
	if balances["debtor"] is not None and balances["creditor"] is not None:
		gap = flt(balances["debtor"] + balances["creditor"], 2)
	items.sort(key=lambda i: (i["posting_date"], i["source_name"]))
	summary = {
		"documents": len(by_key),
		"sent": flt(sum(flt(s["amount"]) for s in senders), 2),
		**{
			_slug(status): {"count": t["count"], "amount": flt(t["amount"], 2)}
			for status, t in totals.items()
		},
		**balances,
		"gap": gap,
		"explained": flt(explained, 2),
		"unexplained": None if gap is None else flt(gap - explained, 2),
		"items": len(items),
		"truncated": len(items) > MAX_ROWS,
	}
	return {"summary": summary, "items": items[:MAX_ROWS]}


def _item(doc, status, sent, received, difference, receivers, notes) -> dict:
	return {
		"kind": KIND[doc["doctype"]],
		"status": status,
		"source_doctype": doc["doctype"],
		"source_name": doc["name"],
		"posting_date": str(getdate(doc["posting_date"])),
		"party": doc.get("party") or "",
		"from_gstin": doc.get("from_gstin") or "",
		"to_gstin": doc.get("to_gstin") or "",
		"sent": sent,
		"received": received,
		"difference": difference,
		"receivers": ", ".join(r["name"] for r in receivers[:MAX_RECEIVERS]),
		"note": "; ".join(dict.fromkeys(notes)),
	}


def _slug(status) -> str:
	return status.lower().replace(" ", "_")


# ── server: store what a site sent ──────────────────────────────────────────

COUNTS = tuple(_slug(s) for s in STATUSES)
NUMBERS = ("documents", "sent", "debtor", "creditor", "gap", "explained", "unexplained", "items")
ACCOUNTS = ("debtor_account", "creditor_account")


def store(data, own) -> dict:
	"""Replace one company's internal transfers with the list its site sent. A summary of None clears them."""
	from accounts_intelligence.auth.server import Rejected
	from accounts_intelligence.sync.server import _upsert

	if not isinstance(data, dict) or not isinstance(data.get("entity"), str) or data["entity"] not in own:
		raise Rejected("internal transfers are not for a company of this site")
	entity, summary, items = data["entity"], data.get("summary"), data.get("items") or []
	if summary is not None and not isinstance(summary, dict):
		raise Rejected("internal transfer summary is not valid")
	if not isinstance(items, list) or len(items) > MAX_ROWS:
		raise Rejected("internal transfer list is not valid")
	try:
		cleaned = _clean_summary(summary) if summary is not None else None
		rows = [_clean_item(entity, item) for item in items] if cleaned else []
	except (AttributeError, KeyError, OverflowError, TypeError, ValueError) as e:
		raise Rejected(f"internal transfers are not valid: {str(e)[:100]}")

	frappe.db.delete(INTERNAL_TRANSFER, {"entity": entity})
	_upsert(INTERNAL_TRANSFER, list({r["name"]: r for r in rows}.values()))
	if cleaned:
		cleaned["received_on"] = str(now_datetime())[:19]
	frappe.db.set_value(
		ENTITY, entity, "internal_transfers", json.dumps(cleaned) if cleaned else None, update_modified=False
	)
	return {"items": len(rows)}


def _number(value, allow_none=True):
	if value is None and allow_none:
		return None
	number = float(value)
	if number != number or number in (float("inf"), float("-inf")):
		raise ValueError("amount is not a number")
	return round(number, 2)


def _clean_summary(summary) -> dict:
	result = {key: _number(summary.get(key)) for key in NUMBERS}
	for key in COUNTS:
		value = summary.get(key) or {}
		count = int(value.get("count") or 0)
		if not 0 <= count <= MAX_COUNT:
			raise ValueError("count is out of range")
		result[key] = {"count": count, "amount": _number(value.get("amount") or 0)}
	for key in ACCOUNTS:
		result[key] = plain(summary.get(key)) or None
	result["truncated"] = bool(summary.get("truncated"))
	return result


def _clean_item(entity, item) -> dict:
	if not isinstance(item, dict):
		raise ValueError("row is not an object")
	doctype = item["source_doctype"]
	if doctype not in KIND or item["status"] not in STATUSES:
		raise ValueError("type or status is not allowed")
	values = {
		"entity": entity,
		"status": item["status"],
		"kind": KIND[doctype],
		"source_doctype": doctype,
		"source_name": plain(item["source_name"]),
		"posting_date": date.fromisoformat(item["posting_date"]),
		"party": plain(item.get("party")),
		"from_gstin": plain(item.get("from_gstin"), 15),
		"to_gstin": plain(item.get("to_gstin"), 15),
		"sent": _number(item.get("sent"), allow_none=False),
		"received": _number(item.get("received"), allow_none=False),
		"difference": _number(item.get("difference"), allow_none=False),
		"receivers": plain(item.get("receivers"), 500),
		"note": plain(item.get("note"), 500),
	}
	if not values["source_name"]:
		raise ValueError("document name is missing")
	values["name"] = row_key(entity, doctype, values["source_name"])
	return values


# ── server: what the dashboard shows ────────────────────────────────────────


def overview() -> list:
	"""Each group company whose site sent internal transfers, with its totals."""
	result = []
	for e in frappe.get_all(
		ENTITY,
		{"internal_transfers": ("is", "set")},
		["name", "company_name", "site_url", "internal_transfers"],
		order_by="company_name",
	):
		try:
			summary = json.loads(e.internal_transfers)
		except ValueError:
			continue
		result.append(
			{"company": {"pan": e.name, "name": e.company_name, "site_url": e.site_url}, "summary": summary}
		)
	return result


@frappe.whitelist(methods=["GET"])
def transfers() -> dict:
	"""Every company's internal transfer totals and the transfers that need a look."""
	roles.check()
	return {
		"companies": overview(),
		"items": frappe.get_all(
			INTERNAL_TRANSFER,
			fields=[
				"entity",
				"status",
				"kind",
				"source_doctype",
				"source_name",
				"posting_date",
				"party",
				"from_gstin",
				"to_gstin",
				"sent",
				"received",
				"difference",
				"receivers",
				"note",
			],
			order_by="posting_date asc, source_name asc",
			limit_page_length=0,
		),
	}

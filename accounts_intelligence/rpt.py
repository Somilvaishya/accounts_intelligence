"""Related-party transactions: what each group company did with each of its related parties in a period.

A company's related parties are the other group companies, and the parties in the related-party register
that are related to it: key managerial personnel, their relatives, the entities they control, a holding
company and so on. A register entry counts only between its Related From and Related Until dates, and only
for its Related To company when one is set.

The figures come from the synced document headers, in company currency:
- sales and purchases without tax, returns netted off;
- money received and paid through payment entries;
- the net of journal entries (debit to the party less credit);
- the closing balance with the party on the last day of the period (debit positive: the party owes).

Remuneration paid through payroll is not synced, so it is not part of these figures.
"""

import frappe
from frappe import _
from frappe.utils import getdate, today

from accounts_intelligence.sync import BALANCE, ENTITY, RELATED_PARTY, TRANSACTION

GROUP = "Group company"
COLUMNS = ("sales", "purchases", "received", "paid", "journals")


@frappe.whitelist(methods=["GET"])
def report(from_date=None, to_date=None, company=None) -> dict:
	"""Related-party transactions per group company and party. The period defaults to this financial
	year (April to March) up to today."""
	frappe.only_for("System Manager")
	from_date, to_date = _period(from_date, to_date)
	entities = {
		e.name: e for e in frappe.get_all(ENTITY, fields=["name", "company_name"], order_by="company_name")
	}
	if company and company not in entities:
		frappe.throw(_("{0} is not a group company.").format(company))
	register = {
		r.name: r
		for r in frappe.get_all(
			RELATED_PARTY,
			fields=["name", "party_name", "relationship", "related_to", "related_from", "related_until"],
		)
	}

	rows = {}

	def row(entity, party):
		key = (entity, party["pan"])
		if key not in rows:
			rows[key] = {
				"company": {"pan": entity, "name": entities[entity].company_name},
				"party": party,
				**dict.fromkeys(COLUMNS, 0.0),
				"documents": 0,
				"balance": None,
			}
		return rows[key]

	for t in _transactions(from_date, to_date, company):
		party = _party(t.entity, t.counterparty, entities, register)
		if not party or not _related_on(party, t.posting_date, t.posting_date):
			continue
		target = row(t.entity, party)
		target[_column(t)] += _value(t)
		target["documents"] += 1

	for (entity, counterparty), balance in _closing_balances(to_date, company).items():
		party = _party(entity, counterparty, entities, register)
		if party and _related_on(party, from_date, to_date):
			row(entity, party)["balance"] = balance

	order = {name: i for i, name in enumerate(relationships())}
	result = sorted(
		rows.values(),
		key=lambda r: (order.get(r["party"]["relationship"], 99), r["company"]["name"], r["party"]["name"]),
	)
	for r in result:
		for column in COLUMNS:
			r[column] = round(r[column], 2)
	return {
		"from_date": str(from_date),
		"to_date": str(to_date),
		"company": company,
		"companies": [{"pan": pan, "name": e.company_name} for pan, e in entities.items()],
		"relationships": relationships(),
		"register_size": len(register),
		"rows": result,
	}


def relationships() -> list[str]:
	"""Group companies first, then the register's relationships in their form order."""
	options = frappe.get_meta(RELATED_PARTY).get_field("relationship").options or ""
	return [GROUP, *(o for o in options.split("\n") if o)]


def _period(from_date, to_date):
	to_date = getdate(to_date or today())
	if from_date:
		from_date = getdate(from_date)
	else:
		# The Indian financial year starts on 1 April.
		start_year = to_date.year if to_date.month >= 4 else to_date.year - 1
		from_date = getdate(f"{start_year}-04-01")
	if from_date > to_date:
		frappe.throw(_("The period starts after it ends."))
	return from_date, to_date


def _transactions(from_date, to_date, company) -> list:
	return frappe.db.sql(
		f"""select entity, counterparty, source_doctype, payment_type, posting_date,
			grand_total, net_total, base_grand_total
		from `tab{TRANSACTION}`
		where is_cancelled = 0 and entity != counterparty
			and posting_date between %(from_date)s and %(to_date)s
			{"and entity = %(company)s" if company else ""}""",
		{"from_date": from_date, "to_date": to_date, "company": company},
		as_dict=True,
	)


def _party(entity, counterparty, entities, register) -> dict | None:
	"""The counterparty as a related party of `entity`, or None when it is not one."""
	if counterparty in entities and counterparty != entity:
		return {"pan": counterparty, "name": entities[counterparty].company_name, "relationship": GROUP}
	entry = register.get(counterparty)
	if entry and (not entry.related_to or entry.related_to == entity):
		return {
			"pan": counterparty,
			"name": entry.party_name,
			"relationship": entry.relationship,
			"related_from": str(entry.related_from) if entry.related_from else None,
			"related_until": str(entry.related_until) if entry.related_until else None,
		}
	return None


def _related_on(party, start, end) -> bool:
	"""Whether the relationship covers any day from `start` to `end`."""
	if party.get("related_from") and getdate(party["related_from"]) > getdate(end):
		return False
	if party.get("related_until") and getdate(party["related_until"]) < getdate(start):
		return False
	return True


def _column(t) -> str:
	if t.source_doctype == "Sales Invoice":
		return "sales"
	if t.source_doctype == "Purchase Invoice":
		return "purchases"
	if t.source_doctype == "Payment Entry":
		return "received" if t.payment_type == "Receive" else "paid"
	return "journals"


def _value(t) -> float:
	"""The amount in company currency: without tax for an invoice, the total for anything else."""
	if t.source_doctype in ("Sales Invoice", "Purchase Invoice") and t.grand_total:
		return (t.net_total or 0) * (t.base_grand_total or 0) / t.grand_total
	return t.base_grand_total or 0


def _closing_balances(to_date, company) -> dict:
	"""Each company's balance with each counterparty on `to_date`: the latest figure sent on or before it,
	added up over the counterparty's customer and supplier accounts."""
	rows = frappe.db.sql(
		f"""select x.entity, x.counterparty, sum(x.balance) as balance
		from `tab{BALANCE}` x
		join (select entity, party_type, party, max(as_of) as as_of from `tab{BALANCE}`
			where as_of <= %(to_date)s {"and entity = %(company)s" if company else ""}
			group by entity, party_type, party) latest
			on latest.entity = x.entity and latest.party_type = x.party_type and latest.party = x.party
			and latest.as_of = x.as_of
		group by x.entity, x.counterparty""",
		{"to_date": to_date, "company": company},
		as_dict=True,
	)
	return {(r.entity, r.counterparty): round(r.balance, 2) for r in rows}

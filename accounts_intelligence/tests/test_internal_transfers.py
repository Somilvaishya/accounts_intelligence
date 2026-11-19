"""Tests for internal transfers: goods one branch of a company sends to another through Business Needed
Solutions, checked against what the receiving branch booked and against the internal debtor and creditor."""

import json
from unittest.mock import patch

import frappe
from frappe.utils import today

from accounts_intelligence import internal_transfers as it
from accounts_intelligence.auth import keys
from accounts_intelligence.auth.server import Rejected
from accounts_intelligence.sync import ENTITY
from accounts_intelligence.sync import server as sync_server
from accounts_intelligence.tests.test_auth import FrappeTestCase, _reset_settings, _row
from accounts_intelligence.tests.test_sync import OTHER_PAN, OWN_PAN, _register_other_company, _request

INTERNAL = {("Customer", "Own Branch"), ("Supplier", "Own Branch")}
NO_LEDGER = {"debtor_account": None, "creditor_account": None, "debtor": None, "creditor": None}


def _sent(doctype, name, amount, ref=None, party="Own Branch", day="2026-10-01"):
	return {
		"doctype": doctype,
		"name": name,
		"posting_date": day,
		"amount": amount,
		"party": party,
		"ref": ref,
		"from_gstin": "09AANCR4593B1Z2",
		"to_gstin": "07AANCR4593B1Z6",
	}


def _got(doctype, name, amount, ref=None, docstatus=1, party="Own Branch"):
	return {**_sent(doctype, name, amount, ref, party), "docstatus": docstatus}


def _assess(senders, receivers, links=None, balances=NO_LEDGER, billed=frozenset()):
	return it.assess(senders, receivers, links or {}, INTERNAL, 1.0, balances, billed=billed)


def _by_name(result):
	return {i["source_name"]: i for i in result["items"]}


class TestAssess(FrappeTestCase):
	def test_a_transfer_booked_on_both_sides_is_not_listed(self):
		result = _assess(
			[_sent("Sales Invoice", "SI-1", 1000), _sent("Delivery Note", "DN-1", 500, ref="PR-1")],
			[_got("Purchase Invoice", "PI-1", 1000.4, ref="SI-1"), _got("Purchase Receipt", "PR-1", 500)],
		)
		self.assertEqual(result["items"], [])
		self.assertEqual(result["summary"]["documents"], 2)
		self.assertEqual(result["summary"]["not_received"]["count"], 0)

	def test_the_link_counts_from_either_side_and_from_the_receipt_items(self):
		"""BNS often fills the reference on only one of the two documents."""
		result = _assess(
			[
				_sent("Sales Invoice", "SI-FWD", 100, ref="PI-FWD"),
				_sent("Sales Invoice", "SI-BACK", 200),
				_sent("Delivery Note", "DN-ITEMS", 300),
			],
			[
				_got("Purchase Invoice", "PI-FWD", 100),
				_got("Purchase Invoice", "PI-BACK", 200, ref="SI-BACK"),
				_got("Purchase Receipt", "PR-ITEMS", 300),
			],
			links={"PR-ITEMS": ["DN-ITEMS"]},
		)
		self.assertEqual(result["items"], [])

	def test_a_transfer_nobody_received_is_listed_with_its_draft(self):
		result = _assess(
			[_sent("Sales Invoice", "SI-1", 3308085.42), _sent("Delivery Note", "DN-1", 30000)],
			[_got("Purchase Invoice", "PI-DRAFT", 3308085.42, ref="SI-1", docstatus=0)],
		)
		items = _by_name(result)
		self.assertEqual(items["SI-1"]["status"], "Not received")
		self.assertIn("Purchase Invoice PI-DRAFT is in draft", items["SI-1"]["note"])
		self.assertEqual((items["SI-1"]["sent"], items["SI-1"]["received"]), (3308085.42, 0))
		self.assertEqual(items["DN-1"]["kind"], "Delivery")
		self.assertEqual(result["summary"]["not_received"], {"count": 2, "amount": 3338085.42})

	def test_received_twice_or_for_another_amount_is_listed_with_the_wrong_party(self):
		result = _assess(
			[
				_sent("Sales Invoice", "SI-18", 2556821.22, ref="PI-OUTSIDE"),
				_sent("Sales Invoice", "SI-2", 500),
			],
			[
				_got("Purchase Invoice", "PI-OUTSIDE", 1731796.66, party="Radha Kishan Dal Mill"),
				_got("Purchase Invoice", "PI-9", 2556821.22, ref="SI-18"),
				_got("Purchase Invoice", "PI-2", 450, ref="SI-2"),
			],
		)
		items = _by_name(result)
		self.assertEqual(items["SI-18"]["status"], "Amount differs")
		self.assertEqual(items["SI-18"]["difference"], -1731796.66)
		self.assertEqual(items["SI-18"]["receivers"], "PI-OUTSIDE, PI-9")
		self.assertIn("PI-OUTSIDE is booked to supplier Radha Kishan Dal Mill", items["SI-18"]["note"])
		self.assertIn("received in 2 documents", items["SI-18"]["note"])
		self.assertEqual(items["SI-2"]["difference"], 50)

	def test_a_delivery_note_billed_by_an_invoice_is_settled_through_the_invoice(self):
		"""DN -> SI -> PI with the stock on a PR: the invoices carry the amount, so the delivery note is not
		reported, received on a purchase receipt or not."""
		result = _assess(
			[
				_sent("Delivery Note", "DN-BILLED", 168000),
				_sent("Delivery Note", "DN-BILLED-2", 184000, ref="PR-2"),
				_sent("Sales Invoice", "SI-1", 168000),
				_sent("Sales Invoice", "SI-2", 184000),
			],
			[
				_got("Purchase Invoice", "PI-1", 168000, ref="SI-1"),
				_got("Purchase Invoice", "PI-2", 184000, ref="SI-2"),
				_got("Purchase Receipt", "PR-2", 184000),
			],
			billed={"DN-BILLED", "DN-BILLED-2"},
		)
		self.assertEqual(result["items"], [])
		self.assertEqual(result["summary"]["documents"], 2)

		# Without its purchase invoice, the invoice is what is not received.
		result = _assess(
			[_sent("Delivery Note", "DN-1", 50), _sent("Sales Invoice", "SI-1", 50)], [], billed={"DN-1"}
		)
		self.assertEqual([i["source_name"] for i in result["items"]], ["SI-1"])

	def test_one_receipt_for_several_deliveries_is_compared_as_a_whole(self):
		result = _assess(
			[_sent("Delivery Note", "DN-1", 300), _sent("Delivery Note", "DN-2", 200)],
			[_got("Purchase Receipt", "PR-1", 500)],
			links={"PR-1": ["DN-1", "DN-2"]},
		)
		self.assertEqual(result["items"], [])
		self.assertEqual(result["summary"]["explained"], 0)

		result = _assess(
			[_sent("Delivery Note", "DN-1", 300), _sent("Delivery Note", "DN-2", 200)],
			[_got("Purchase Receipt", "PR-1", 450)],
			links={"PR-1": ["DN-1", "DN-2"]},
		)
		[item] = result["items"]
		self.assertEqual((item["source_name"], item["status"], item["sent"]), ("DN-1", "Amount differs", 500))
		self.assertIn("received together with DN-2", item["note"])

	def test_received_in_parts_is_not_a_party_problem(self):
		result = _assess(
			[_sent("Delivery Note", "DN-1", 500)],
			[
				_got("Purchase Receipt", "PR-1", 300, ref="DN-1"),
				_got("Purchase Receipt", "PR-2", 200, ref="DN-1"),
			],
		)
		self.assertEqual(result["items"], [])

	def test_deliveries_allow_bns_five_rupees(self):
		result = _assess(
			[_sent("Delivery Note", "DN-1", 1000), _sent("Delivery Note", "DN-2", 1000)],
			[
				_got("Purchase Receipt", "PR-1", 996, ref="DN-1"),
				_got("Purchase Receipt", "PR-2", 994, ref="DN-2"),
			],
		)
		self.assertEqual(list(_by_name(result)), ["DN-2"])

	def test_matching_amounts_with_an_outside_party_need_a_check(self):
		result = _assess(
			[_sent("Sales Invoice", "SI-1", 100, party="Outside Customer")],
			[_got("Purchase Invoice", "PI-1", 100, ref="SI-1")],
		)
		[item] = result["items"]
		self.assertEqual(item["status"], "Check party")
		self.assertIn("customer Outside Customer is not an internal customer", item["note"])

	def test_a_receipt_without_a_sending_document_is_not_dispatched(self):
		result = _assess(
			[_sent("Delivery Note", "DN-1", 100)],
			[
				_got("Purchase Receipt", "PR-1", 100, ref="DN-1"),
				_got("Purchase Receipt", "PR-LOST", 700, ref="DN-CANCELLED"),
				_got("Purchase Receipt", "PR-DRAFT", 50, docstatus=0),
			],
		)
		[item] = result["items"]
		self.assertEqual(
			(item["source_name"], item["status"], item["difference"]), ("PR-LOST", "Not dispatched", -700)
		)
		self.assertIn("names DN-CANCELLED", item["note"])

	def test_the_listed_transfers_explain_the_ledger_gap(self):
		"""Each transfer adds what was sent less what was received to Internal Debtor plus Internal Creditor."""
		senders = [
			_sent("Sales Invoice", "SI-1", 1000, ref="PI-1"),
			_sent("Sales Invoice", "SI-2", 400),
			_sent("Delivery Note", "DN-1", 300),
		]
		receivers = [
			_got("Purchase Invoice", "PI-1", 1000),
			_got("Purchase Receipt", "PR-X", 50, ref="DN-GONE"),
		]
		ledger = {"debtor_account": "Internal Debtor", "creditor_account": "Internal Creditor"}
		balances = {**ledger, "debtor": 1700, "creditor": -1050}
		summary = _assess(senders, receivers, balances=balances)["summary"]
		self.assertEqual((summary["gap"], summary["explained"], summary["unexplained"]), (650, 650, 0))

		summary = _assess(senders, receivers, balances={**balances, "creditor": -1000})["summary"]
		self.assertEqual(summary["unexplained"], 50, "a credit no listed transfer accounts for")
		self.assertIsNone(_assess(senders, receivers)["summary"]["unexplained"], "no accounts set in BNS")

	def test_only_the_oldest_500_are_sent_but_all_are_counted(self):
		senders = [_sent("Sales Invoice", f"SI-{n:04}", 10) for n in range(it.MAX_ROWS + 5)]
		result = _assess(senders, [])
		self.assertEqual(len(result["items"]), it.MAX_ROWS)
		self.assertTrue(result["summary"]["truncated"])
		self.assertEqual(result["summary"]["not_received"]["count"], it.MAX_ROWS + 5)

	def test_the_site_reads_its_own_books(self):
		"""The queries run on a site with BNS; without it there is nothing to read."""
		if not it.available():
			self.skipTest("Business Needed Solutions is not installed on this site")
		company = frappe.defaults.get_global_default("company") or frappe.get_all("Company", pluck="name")[0]
		result = it.snapshot(company)
		summary = result["summary"]
		for key in ("documents", "sent", "gap", "explained", "unexplained", "not_received", "truncated"):
			self.assertIn(key, summary)
		self.assertEqual(summary["items"], len(result["items"]))
		if summary["gap"] is not None:
			self.assertAlmostEqual(summary["unexplained"], summary["gap"] - summary["explained"], places=2)
		json.dumps(result)


SUMMARY = {
	"documents": 3,
	"sent": 1700,
	"not_received": {"count": 1, "amount": 400},
	"amount_differs": {"count": 0, "amount": 0},
	"check_party": {"count": 0, "amount": 0},
	"not_dispatched": {"count": 0, "amount": 0},
	"debtor_account": "Internal Debtor - X",
	"creditor_account": "Internal Creditor - X",
	"debtor": 1700,
	"creditor": -1300,
	"gap": 400,
	"explained": 400,
	"unexplained": 0,
	"items": 1,
	"truncated": False,
}
ITEM = {
	"kind": "Invoice",
	"status": "Not received",
	"source_doctype": "Sales Invoice",
	"source_name": "SI-2",
	"posting_date": "2026-10-15",
	"party": "Own Branch",
	"from_gstin": "09AANCR4593B1Z2",
	"to_gstin": "07AANCR4593B1Z6",
	"sent": 400,
	"received": 0,
	"difference": 400,
	"receivers": "",
	"note": "",
}


class TestStore(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		for doctype in (ENTITY, it.INTERNAL_TRANSFER):
			frappe.db.delete(doctype)
		_register_other_company()
		sync_server.config_handler(_request({"companies": [{"company": "Alpha", "pan": OWN_PAN}]}))

	def _sync(self, internal):
		return sync_server.sync_handler(_request({"internal": internal}, row_name=_row().name))

	def test_the_latest_list_replaces_the_previous_one(self):
		reply = self._sync(
			{"entity": OWN_PAN, "summary": SUMMARY, "items": [ITEM, {**ITEM, "source_name": "SI-3"}]}
		)
		self.assertEqual(reply["internal"], {"items": 2})
		self._sync({"entity": OWN_PAN, "summary": SUMMARY, "items": [ITEM]})
		self.assertEqual(frappe.get_all(it.INTERNAL_TRANSFER, pluck="source_name"), ["SI-2"])

		[company] = it.overview()
		self.assertEqual(company["company"]["pan"], OWN_PAN)
		self.assertEqual(company["summary"]["not_received"], {"count": 1, "amount": 400})
		self.assertTrue(company["summary"]["received_on"])

		data = it.transfers()
		self.assertEqual(
			[(i.entity, i.status, i.difference) for i in data["items"]], [(OWN_PAN, "Not received", 400)]
		)

	def test_a_site_that_stops_using_bns_clears_its_list(self):
		self._sync({"entity": OWN_PAN, "summary": SUMMARY, "items": [ITEM]})
		self._sync({"entity": OWN_PAN, "summary": None, "items": []})
		self.assertEqual(it.overview(), [])
		self.assertEqual(frappe.db.count(it.INTERNAL_TRANSFER), 0)

	def test_a_site_can_only_send_its_own_company(self):
		for internal in (
			{"entity": OTHER_PAN, "summary": SUMMARY, "items": []},
			{"entity": OWN_PAN, "summary": "x", "items": []},
			{"entity": OWN_PAN, "summary": SUMMARY, "items": [{**ITEM, "status": "Fine"}]},
			{"entity": OWN_PAN, "summary": SUMMARY, "items": [{**ITEM, "source_doctype": "Journal Entry"}]},
			{"entity": OWN_PAN, "summary": SUMMARY, "items": [ITEM] * (it.MAX_ROWS + 1)},
			{"entity": [OWN_PAN], "summary": SUMMARY, "items": []},
			{"entity": OWN_PAN, "summary": {**SUMMARY, "not_received": 5}, "items": []},
			{"entity": OWN_PAN, "summary": {**SUMMARY, "not_received": {"count": float("inf")}}, "items": []},
			{"entity": OWN_PAN, "summary": {**SUMMARY, "not_received": {"count": -1}}, "items": []},
			{"entity": OWN_PAN, "summary": {**SUMMARY, "gap": "NaN"}, "items": []},
			"not a dict",
		):
			with self.subTest(internal=str(internal)[:60]), self.assertRaises(Rejected):
				self._sync(internal)
		self.assertEqual(frappe.db.count(it.INTERNAL_TRANSFER), 0)

	def test_text_is_stored_plain(self):
		self._sync({"entity": OWN_PAN, "summary": SUMMARY, "items": [{**ITEM, "note": "<b>x</b>\nnext"}]})
		self.assertNotIn("<", frappe.db.get_value(it.INTERNAL_TRANSFER, {}, "note"))

	def test_the_dashboard_carries_the_totals(self):
		from accounts_intelligence import reconcile

		self._sync({"entity": OWN_PAN, "summary": SUMMARY, "items": [ITEM]})
		self.assertEqual([c["company"]["pan"] for c in reconcile.dashboard()["internal"]], [OWN_PAN])


class TestSend(FrappeTestCase):
	def _send(self, state, found=None, bns=True, an_hour_later=True):
		posted = []
		if an_hour_later:
			state.pop("internal_checked", None)

		def post(settings, **batch):
			posted.append(batch["internal"])
			return []

		found = found or {"summary": dict(SUMMARY), "items": [ITEM]}
		with (
			patch.object(it, "available", return_value=bns),
			patch.object(it, "snapshot", return_value=found),
		):
			it.send(None, state, {"Alpha": OWN_PAN}, post)
		return posted

	def test_sent_when_changed_and_once_a_day(self):
		state = {}
		[payload] = self._send(state)
		self.assertEqual((payload["entity"], payload["items"]), (OWN_PAN, [ITEM]))
		self.assertEqual(self._send(state), [], "unchanged the same day")
		self.assertEqual(len(self._send(state, {"summary": dict(SUMMARY), "items": []})), 1, "changed")
		state["internal_on"] = "2000-01-01"
		self.assertEqual(len(self._send(state, {"summary": dict(SUMMARY), "items": []})), 1, "a new day")
		self.assertEqual(state["internal_on"], today())

	def test_a_site_without_bns_sends_nothing_unless_it_sent_before(self):
		self.assertEqual(self._send({}, bns=False), [])
		state = {}
		self._send(state)
		[payload] = self._send(state, bns=False)
		self.assertEqual((payload["summary"], payload["items"]), (None, []))
		self.assertIsNone(state["internal"])
		self.assertEqual(self._send(state, bns=False), [])

	def test_checked_at_most_once_an_hour(self):
		state = {}
		self.assertEqual(len(self._send(state)), 1)
		changed = {"summary": dict(SUMMARY), "items": []}
		self.assertEqual(self._send(state, changed, an_hour_later=False), [], "checked minutes ago")
		self.assertEqual(len(self._send(state, changed)), 1)

	def test_a_failed_check_is_noted_and_the_sync_goes_on(self):
		from accounts_intelligence.auth import transport
		from accounts_intelligence.sync import client

		for error, note in (
			(RuntimeError("boom"), "could not be checked"),
			(transport.TransportError("refused"), "refused"),
		):
			with (
				self.subTest(note=note),
				patch.object(it, "send", side_effect=error),
				patch.object(client, "_save_state"),
			):
				[message] = client._send_internal(None, {}, {"Alpha": OWN_PAN})
				self.assertIn(note, message)

	def test_a_site_with_no_transfers_sends_nothing(self):
		empty = {"summary": {**SUMMARY, "documents": 0, "gap": None}, "items": []}
		self.assertEqual(self._send({}, empty), [])

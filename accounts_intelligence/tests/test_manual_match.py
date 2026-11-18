"""Tests for reconciling by hand: entries the automatic matching leaves open, such as the same amount booked
on different days, matched from the pair page, one to one or many to many, and undone."""

import json

import frappe

from accounts_intelligence import reconcile
from accounts_intelligence.reconcile import MATCH, MATCH_DOCUMENT
from accounts_intelligence.sync import TRANSACTION
from accounts_intelligence.tests.test_auth import FrappeTestCase
from accounts_intelligence.tests.test_reconcile import A, B, _add, _status, two_companies


def _doc(entity, doctype, name):
	return {"entity": entity, "doctype": doctype, "name": name}


def _match(*documents, note="Checked with the bank statement"):
	return reconcile.match(A, B, json.dumps(list(documents)), note)["name"]


def _manual(result):
	return [line for line in result["lines"] if line.get("match")]


class TestManualMatch(FrappeTestCase):
	def setUp(self):
		frappe.set_user("Administrator")
		two_companies()
		frappe.db.delete(MATCH)
		frappe.db.delete(MATCH_DOCUMENT)
		# Two payments of 400 from A, received by B a week or more later: too far apart to match on their own.
		_add(A, "Payment Entry", "PAY-A1", "2026-09-01", 400, payment_type="Pay")
		_add(A, "Payment Entry", "PAY-A2", "2026-09-15", 400, payment_type="Pay")
		_add(B, "Payment Entry", "REC-B1", "2026-09-10", 400, payment_type="Receive")
		_add(B, "Payment Entry", "REC-B2", "2026-09-28", 400, payment_type="Receive")
		# One sales invoice of 800 from A, booked by B as two purchase invoices on other days, without bill numbers.
		_add(A, "Sales Invoice", "SI-800", "2026-09-02", 800)
		_add(B, "Purchase Invoice", "PI-400-1", "2026-09-06", 400)
		_add(B, "Purchase Invoice", "PI-400-2", "2026-09-20", 400)

	def test_the_same_amount_on_far_dates_stays_open_until_matched_by_hand(self):
		before = reconcile.reconcile(A, B)
		for name in ("PAY-A1", "PAY-A2"):
			self.assertEqual(_status(before, name), "only_a")
		for name in ("REC-B1", "REC-B2"):
			self.assertEqual(_status(before, name), "only_b")

		name = _match(
			_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"), note="Same transfer"
		)
		after = reconcile.reconcile(A, B)
		[line] = _manual(after)
		self.assertEqual(line["status"], "matched")
		self.assertEqual((line["a"]["name"], line["b"]["name"]), ("PAY-A1", "REC-B1"))
		self.assertEqual(line["difference"], 0)
		self.assertEqual(line["date"], "2026-09-01")
		self.assertEqual(line["match"]["name"], name)
		self.assertEqual(line["match"]["by"], "Administrator")
		self.assertEqual(line["match"]["note"], "Same transfer")
		self.assertEqual(_status(after, "PAY-A2"), "only_a", "the other payment is still open")
		self.assertEqual(after["summary"]["money"]["matched"], 1)
		self.assertEqual(after["summary"]["unexplained"], before["summary"]["unexplained"])

	def test_many_to_many_invoices(self):
		_match(
			_doc(A, "Sales Invoice", "SI-800"),
			_doc(B, "Purchase Invoice", "PI-400-1"),
			_doc(B, "Purchase Invoice", "PI-400-2"),
		)
		[line] = _manual(reconcile.reconcile(A, B))
		self.assertEqual(line["kind"], "Invoice")
		self.assertEqual([d["name"] for d in line["a_docs"]], ["SI-800"])
		self.assertEqual([d["name"] for d in line["b_docs"]], ["PI-400-1", "PI-400-2"])
		self.assertEqual(line["difference"], 0)
		record = frappe.get_doc(MATCH, line["match"]["name"])
		self.assertEqual((record.kind, record.amount, len(record.documents)), ("Invoice", 800, 3))

	def test_only_open_documents_whose_totals_agree_can_be_matched(self):
		_add(A, "Sales Invoice", "SI-AUTO", "2026-09-03", 250)
		_add(B, "Purchase Invoice", "PI-AUTO", "2026-09-03", 250)
		_add(B, "Sales Invoice", "SI-B", "2026-09-04", 400)
		refused = {
			"the totals differ": [
				_doc(A, "Sales Invoice", "SI-800"),
				_doc(B, "Purchase Invoice", "PI-400-1"),
			],
			"one company only": [_doc(A, "Payment Entry", "PAY-A1"), _doc(A, "Payment Entry", "PAY-A2")],
			"an invoice with a payment": [
				_doc(A, "Payment Entry", "PAY-A1"),
				_doc(B, "Purchase Invoice", "PI-400-1"),
			],
			"two sales invoices": [_doc(A, "Sales Invoice", "SI-800"), _doc(B, "Sales Invoice", "SI-B")],
			"a matched document": [
				_doc(A, "Sales Invoice", "SI-AUTO"),
				_doc(B, "Purchase Invoice", "PI-AUTO"),
			],
			"an unknown document": [_doc(A, "Payment Entry", "PAY-X"), _doc(B, "Payment Entry", "REC-B1")],
			"a document twice": [_doc(A, "Payment Entry", "PAY-A1"), _doc(A, "Payment Entry", "PAY-A1")],
		}
		for case, documents in refused.items():
			with self.subTest(case), self.assertRaises(frappe.ValidationError):
				_match(*documents)
		self.assertEqual(frappe.db.count(MATCH), 0)

	def test_a_matched_document_cannot_be_matched_again(self):
		_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		with self.assertRaises(frappe.ValidationError):
			_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B2"))

	def test_undo_opens_the_documents_again(self):
		name = _match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		reconcile.unmatch(name)
		result = reconcile.reconcile(A, B)
		self.assertEqual(_manual(result), [])
		self.assertEqual(_status(result, "PAY-A1"), "only_a")
		with self.assertRaises(frappe.ValidationError):
			reconcile.unmatch(name)

	def test_a_cancelled_document_breaks_the_match_and_says_so(self):
		name = _match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		row = frappe.db.get_value(TRANSACTION, {"source_name": "REC-B1"}, "name")
		frappe.db.set_value(TRANSACTION, row, "is_cancelled", 1)
		result = reconcile.reconcile(A, B)
		self.assertEqual(_manual(result), [], "a match whose document is cancelled no longer holds")
		self.assertEqual(_status(result, "PAY-A1"), "only_a")
		[broken] = result["broken_matches"]
		self.assertEqual(broken["name"], name)
		self.assertIn("REC-B1", broken["reason"])
		self.assertEqual([d["name"] for d in broken["documents"]], ["PAY-A1", "REC-B1"])
		reconcile.unmatch(name)
		self.assertEqual(reconcile.reconcile(A, B)["broken_matches"], [])

	def test_a_changed_amount_breaks_the_match_and_says_so(self):
		_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		row = frappe.db.get_value(TRANSACTION, {"source_name": "REC-B1"}, "name")
		frappe.db.set_value(TRANSACTION, row, "base_grand_total", 450)
		result = reconcile.reconcile(A, B)
		self.assertEqual(_manual(result), [])
		[broken] = result["broken_matches"]
		self.assertIn("no longer agree", broken["reason"])

	def test_a_document_in_two_matches_counts_once(self):
		"""Two people matching the same entries at once: the lock makes the second wait and refuses it, and a
		duplicate made anyway (an older copy of the data) only counts in the oldest match."""
		first = _match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		copy = frappe.copy_doc(frappe.get_doc(MATCH, first))
		copy.insert(ignore_permissions=True)
		result = reconcile.reconcile(A, B)
		[line] = _manual(result)
		self.assertEqual(line["match"]["name"], first)
		self.assertEqual([m["name"] for m in result["broken_matches"]], [copy.name])

	def test_a_note_is_required(self):
		for note in (None, "", "   "):
			with self.subTest(note=note), self.assertRaises(frappe.ValidationError):
				_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"), note=note)

	def test_the_summary_counts_matches_made_by_hand(self):
		self.assertEqual(reconcile.reconcile(A, B)["summary"]["manual"], 0)
		_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		_match(
			_doc(A, "Sales Invoice", "SI-800"),
			_doc(B, "Purchase Invoice", "PI-400-1"),
			_doc(B, "Purchase Invoice", "PI-400-2"),
		)
		self.assertEqual(reconcile.reconcile(A, B)["summary"]["manual"], 2)

	def test_the_assistant_finds_every_document_of_a_match(self):
		from accounts_intelligence import assistant

		_match(
			_doc(A, "Sales Invoice", "SI-800"),
			_doc(B, "Purchase Invoice", "PI-400-1"),
			_doc(B, "Purchase Invoice", "PI-400-2"),
			note="One order booked as two bills",
		)
		facts, _lines = assistant.find_document(assistant._companies(), "PI-400-2")
		self.assertEqual(facts["lines_found"], 1)
		[line] = facts["lines"]
		self.assertIn("One order booked as two bills", line["reconciled_by_hand"])
		self.assertEqual(
			sum(len(v) for v in line.values() if isinstance(v, list)), 2, "both purchase invoices"
		)

	def test_a_period_shows_the_whole_match_when_one_document_is_in_it(self):
		_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		result = reconcile.reconcile(A, B, "2026-09-05", "2026-09-30")
		[line] = _manual(result)
		self.assertEqual((line["a"]["name"], line["b"]["name"]), ("PAY-A1", "REC-B1"))
		self.assertEqual(line["date"], "2026-09-10", "dated by its document inside the period")
		names = [d["name"] for x in result["lines"] for d in (x["a"], x["b"]) if d]
		self.assertEqual(names.count("PAY-A1"), 1, "the document outside the period shows only in its match")

		later = reconcile.reconcile(A, B, "2026-09-11", "2026-09-30")
		self.assertEqual(_manual(later), [])
		self.assertEqual(later["broken_matches"], [], "a match outside the period is out of view, not broken")
		names = [d["name"] for x in later["lines"] for d in (x["a"], x["b"]) if d]
		self.assertNotIn("PAY-A1", names)
		self.assertNotIn("REC-B1", names)

	def test_the_line_key_is_stable_and_open_items_skip_the_match(self):
		_match(_doc(A, "Payment Entry", "PAY-A1"), _doc(B, "Payment Entry", "REC-B1"))
		[one] = _manual(reconcile.reconcile(A, B))
		[two] = _manual(reconcile.reconcile(B, A))
		self.assertEqual(one["key"], two["key"])
		self.assertIsNone(one["review"])

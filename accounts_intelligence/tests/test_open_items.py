"""Tests for the open items work queue: listing open lines, and saving their reviews."""

import frappe

from accounts_intelligence import open_items, reconcile
from accounts_intelligence.reconcile import OPEN_ITEM
from accounts_intelligence.tests.test_auth import FrappeTestCase
from accounts_intelligence.tests.test_reconcile import A, B, _add, two_companies


class TestOpenItems(FrappeTestCase):
	def setUp(self):
		two_companies()
		frappe.db.delete(OPEN_ITEM)
		_add(A, "Sales Invoice", "SI-1", "2026-10-01", 100)
		_add(A, "Sales Invoice", "SI-2", "2026-10-02", 500)
		_add(B, "Purchase Invoice", "PI-2", "2026-10-02", 500, bill_no="SI-2")

	def _key(self, name):
		return next(i["key"] for i in open_items.items()["items"] if (i["a"] or {}).get("name") == name)

	def test_open_lines_are_listed_with_pair_and_age(self):
		data = open_items.items()
		self.assertEqual([i["a"]["name"] for i in data["items"]], ["SI-1"], "the matched invoice is not open")
		item = data["items"][0]
		self.assertEqual((item["pair"]["a"]["pan"], item["pair"]["b"]["pan"]), (A, B))
		self.assertGreater(item["age"], 0)
		self.assertIsNone(item["review"])
		self.assertIn("Administrator", [u["name"] for u in data["users"]])

	def test_a_review_is_saved_and_shown_with_the_line(self):
		key = self._key("SI-1")
		open_items.review(A, B, key, status="In progress", assigned_to="Administrator")
		open_items.review(
			B, A, key, status="Explained", reason="Will be booked", note="Beta books it in November"
		)
		self.assertEqual(frappe.db.count(OPEN_ITEM), 1, "the same line keeps one review")

		data = reconcile.reconcile(A, B)
		line = next(x for x in data["lines"] if x["key"] == key)
		self.assertEqual(
			line["review"],
			{
				"status": "Explained",
				"assigned_to": None,
				"reason": "Will be booked",
				"note": "Beta books it in November",
			},
		)
		self.assertEqual((data["summary"]["open"], data["summary"]["explained"]), (1, 1))
		doc = frappe.get_doc(OPEN_ITEM, key)
		self.assertEqual(
			(doc.documents, doc.difference, doc.reviewed_by), ("SI-1 ↔ not recorded", 100, "Administrator")
		)

	def test_a_line_has_the_same_key_whichever_way_round_the_pair_is(self):
		self.assertEqual(
			{x["key"] for x in reconcile.reconcile(A, B)["lines"]},
			{x["key"] for x in reconcile.reconcile(B, A)["lines"]},
		)

	def test_only_an_open_line_can_be_reviewed_and_only_properly(self):
		key = self._key("SI-1")
		matched = next(x["key"] for x in reconcile.reconcile(A, B)["lines"] if x["status"] == "matched")
		for args, kwargs in (
			((A, B, matched), {}),
			((A, B, "no-such-line"), {}),
			((A, B, key), {"status": "Closed"}),
			((A, B, key), {"assigned_to": "nobody@example.com"}),
			((A, B, key), {"status": "Explained"}),
			((A, A, key), {}),
		):
			self.assertRaises(frappe.ValidationError, open_items.review, *args, **kwargs)
		self.assertEqual(frappe.db.count(OPEN_ITEM), 0)

	def test_only_a_system_manager_can_use_the_queue(self):
		in_test, frappe.flags.in_test = frappe.flags.in_test, False
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, open_items.items)
			self.assertRaises(frappe.PermissionError, open_items.review, A, B, "x")
		finally:
			frappe.set_user("Administrator")
			frappe.flags.in_test = in_test

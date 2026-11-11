"""Tests for the assistant. The AI calls are replaced, so these check what the model is offered and what the
page gets back, not what a model would say."""

import json
import re
from unittest.mock import patch

import frappe

from accounts_intelligence import assistant, open_items
from accounts_intelligence.reconcile import OPEN_ITEM
from accounts_intelligence.sync import RELATED_PARTY
from accounts_intelligence.tests.test_auth import FrappeTestCase
from accounts_intelligence.tests.test_reconcile import A, B, _add, _balance, two_companies
from accounts_intelligence.tests.test_rpt import DIRECTOR, _tx


def _documents(lines) -> list[str]:
	"""The document numbers in the lines the model is given."""
	return [re.search(r"\b(SI|PI|PAY)-\d+\b", json.dumps(line)).group(0) for line in lines]


class TestAssistant(FrappeTestCase):
	def setUp(self):
		two_companies()
		frappe.db.delete(OPEN_ITEM)
		frappe.db.delete(RELATED_PARTY)
		_add(A, "Sales Invoice", "SI-1", "2026-10-01", 100)
		_add(B, "Purchase Invoice", "PI-1", "2026-10-02", 250, bill_no="SI-7")
		_add(A, "Payment Entry", "PAY-1", "2026-10-05", 5000, payment_type="Pay")
		_balance(A, "Customer", "2026-10-31", 1000)
		_balance(B, "Supplier", "2026-10-31", -900)
		self.prompts = []

	def _ask(self, *replies, question="Which documents did Alpha record that Beta did not?", history=None):
		replies = list(replies)

		def call(prompt, action):
			self.prompts.append(prompt)
			return replies.pop(0)

		with (
			patch.object(assistant, "_unavailable", return_value=None),
			patch.object(assistant, "_check_rate"),
			patch.object(assistant, "_call", side_effect=call),
		):
			return assistant.ask(question, history)

	def test_open_items_are_found_and_the_figures_come_worked_out(self):
		plan = {"tool": "open_items", "args": {"recorded_by": "AL", "status": "missing", "sort": "oldest"}}
		result = self._ask(json.dumps(plan), "Two documents.")
		self.assertTrue(result["ok"])
		self.assertEqual(result["answer"], "Two documents.")
		self.assertEqual([line["a"]["name"] for line in result["lines"]], ["SI-1", "PAY-1"])
		self.assertEqual(result["lines"][0]["status_label"], "only in AL's books")
		self.assertIn("AL: Alpha Ltd", self.prompts[0], "the model is told the companies' short names")
		self.assertIn("₹5,000.00", self.prompts[1], "amounts reach the model already formatted")
		self.assertNotIn("PI-1", self.prompts[1], "Beta's own document is not one Alpha recorded")

	def test_a_pair_summary_says_who_owes_whom(self):
		result = self._ask(json.dumps({"tool": "pair_summary", "args": {"a": "Beta", "b": A}}), "Beta owes.")
		self.assertTrue(result["ok"])
		data = self.prompts[1]
		self.assertIn("BL owes AL ₹1,000.00", data)
		self.assertIn("BL owes AL ₹900.00", data)
		self.assertIn("₹100.00", data, "the balance difference")

	def test_a_document_is_found_by_its_bill_number(self):
		result = self._ask(json.dumps({"tool": "find_document", "args": {"text": "si-7"}}), "Found it.")
		self.assertEqual([line["b"]["name"] for line in result["lines"]], ["PI-1"])

	def test_a_greeting_costs_one_call(self):
		result = self._ask(json.dumps({"tool": "none", "reply": "Hello."}), question="hi")
		self.assertEqual(result, {"ok": True, "answer": "Hello."})
		self.assertEqual(len(self.prompts), 1)

	def test_a_bad_plan_is_refused_before_any_data_is_read(self):
		for plan in (
			"not json",
			json.dumps({"tool": "run_sql", "args": {}}),
			json.dumps({"tool": "pair_summary", "args": {"a": "AL", "b": "Nobody"}}),
			json.dumps({"tool": "pair_summary", "args": {"a": "AL", "b": "AL"}}),
		):
			self.prompts.clear()
			result = self._ask(plan)
			self.assertFalse(result["ok"], plan)
			self.assertEqual(len(self.prompts), 1, f"no answer call after {plan}")

	def test_history_is_trimmed_and_checked(self):
		history = [{"question": f"q{i}", "answer": "a" * 5000} for i in range(9)] + ["junk"]
		turns = assistant._history(json.dumps(history))
		self.assertEqual(len(turns), 3, "the last four entries, less the one that is not a turn")
		self.assertEqual(len(turns[0]["answer"]), 2000)
		self.assertEqual(assistant._history("{not json"), [])

	def test_amounts_use_indian_grouping(self):
		self.assertEqual(assistant._inr(77561783), "₹7,75,61,783.00")
		self.assertEqual(assistant._inr(-14040), "-₹14,040.00")
		self.assertEqual(assistant._inr(5), "₹5.00")

	def test_without_ai_interface_it_says_what_to_install(self):
		with patch.object(frappe, "get_installed_apps", return_value=["frappe"]):
			self.assertEqual(
				assistant.status(),
				{"available": False, "reason": "Install the AI Interface app to use the assistant."},
			)
			self.assertFalse(assistant.ask("hello")["ok"])

	def test_only_a_system_manager_can_ask(self):
		# On v15, frappe.only_for lets everyone through while tests run.
		in_test, frappe.flags.in_test = frappe.flags.in_test, False
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, assistant.ask, "hello")
			self.assertRaises(frappe.PermissionError, assistant.status)
		finally:
			frappe.set_user("Administrator")
			frappe.flags.in_test = in_test

	def test_an_email_draft_lists_what_the_other_company_has_to_look_at(self):
		plan = {"tool": "draft_email", "args": {"a": "AL", "b": "BL"}}
		result = self._ask(
			json.dumps(plan), "Subject: Open items\n\nDear Beta team,\n- SI-1", question="Write to Beta"
		)
		self.assertEqual(result["draft"], {"subject": "Open items", "body": "Dear Beta team,\n- SI-1"})
		self.assertIn("draft to Beta Ltd", result["answer"])
		self.assertIn("accounts team of Alpha Ltd", self.prompts[1])

		facts, _ = assistant.draft_email(assistant._companies(), A, B)
		self.assertEqual(_documents(facts["missing_in_your_books"]), ["SI-1", "PAY-1"])
		self.assertEqual(_documents(facts["in_your_books_not_ours"]), ["PI-1"])
		self.assertEqual(facts["balance_in_our_books"], "BL owes AL ₹1,000.00")

	def test_an_explained_line_is_left_out_of_the_draft_and_shown_in_answers(self):
		key = next(i["key"] for i in open_items.items()["items"] if (i["a"] or {}).get("name") == "SI-1")
		open_items.review(A, B, key, status="Explained", reason="Will be booked", note="in November")
		facts, _ = assistant.draft_email(assistant._companies(), A, B)
		self.assertEqual(len(facts["missing_in_your_books"]), 1, "only PAY-1 is left to ask about")

		self._ask(json.dumps({"tool": "open_items", "args": {}}), "ok")
		self.assertIn("Explained; Will be booked; in November", self.prompts[1])

	def test_a_draft_without_a_subject_line_gets_one(self):
		draft = assistant._split_draft("Dear team, please check.", {"from": "Alpha Ltd", "to": "Beta Ltd"})
		self.assertEqual(draft["subject"], "Reconciliation between Alpha Ltd and Beta Ltd")

	def test_related_party_transactions_come_from_the_rpt_report(self):
		frappe.get_doc(
			{
				"doctype": RELATED_PARTY,
				"pan": DIRECTOR,
				"party_name": "Director One",
				"relationship": "Key managerial personnel",
			}
		).insert(ignore_permissions=True)
		_tx(A, DIRECTOR, "Sales Invoice", "SI-D", "2026-08-01", 1000, tax=180)
		plan = {"tool": "related_party_transactions", "args": {"company": "AL", "from_date": "2026-04-01"}}
		self._ask(json.dumps(plan), "Sold ₹1,000.00 to the director.")
		self.assertIn("Director One", self.prompts[1])
		self.assertIn('"sales": "₹1,000.00"', self.prompts[1], "without tax")

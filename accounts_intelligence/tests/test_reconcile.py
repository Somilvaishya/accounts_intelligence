"""Tests for reconciliation: invoice, money and balance matching between two group companies."""

import frappe

from accounts_intelligence import reconcile
from accounts_intelligence.sync import BALANCE, ENTITY, TRANSACTION
from accounts_intelligence.sync import server as sync_server
from accounts_intelligence.tests.test_auth import FrappeTestCase

A = "AAACA1111A"
B = "AAACB2222B"
PARTY_TYPE = {"Sales Invoice": "Customer", "Purchase Invoice": "Supplier"}


def _add(entity, doctype, name, posting_date, amount, **values):
	row = {
		"entity": entity,
		"counterparty": B if entity == A else A,
		"source_doctype": doctype,
		"source_name": name,
		"posting_date": posting_date,
		"party_type": PARTY_TYPE.get(doctype, "Customer"),
		"party": "Group Party",
		"base_grand_total": amount,
		"grand_total": amount,
		"source_modified": "2026-10-01 10:00:00",
		**values,
	}
	sync_server._upsert(TRANSACTION, [sync_server._transaction(row)], version="source_modified")


def _balance(entity, party_type, as_of, balance):
	row = {
		"entity": entity,
		"counterparty": B if entity == A else A,
		"party_type": party_type,
		"party": "Group Party",
		"as_of": as_of,
		"balance": balance,
	}
	sync_server._upsert(BALANCE, [sync_server._balance(row)])


def _status(result, name):
	"""The status of the line holding the document `name`, on either side."""
	found = [
		line["status"]
		for line in result["lines"]
		if name in ((line["a"] or {}).get("name"), (line["b"] or {}).get("name"))
	]
	return found[0] if len(found) == 1 else found


class TestReconcile(FrappeTestCase):
	def setUp(self):
		for doctype in (ENTITY, TRANSACTION, BALANCE):
			frappe.db.delete(doctype)
		for pan, name in ((A, "Alpha Ltd"), (B, "Beta Ltd")):
			frappe.get_doc(
				{
					"doctype": ENTITY,
					"pan": pan,
					"company_name": name,
					"site_url": f"https://{pan}.example.com",
				}
			).insert(ignore_permissions=True)

	def test_invoices_match_by_bill_number_then_by_date_and_amount(self):
		_add(A, "Sales Invoice", "SI-0001", "2026-10-01", 1180)
		_add(B, "Purchase Invoice", "PI-1", "2026-10-01", 1179.5, bill_no="SI-0001")
		_add(A, "Sales Invoice", "SI-0262", "2026-10-02", 500)
		_add(B, "Purchase Invoice", "PI-2", "2026-10-02", 500, bill_no=" 0262")
		_add(A, "Sales Invoice", "SI-0003", "2026-10-04", 1000)
		_add(B, "Purchase Invoice", "PI-3", "2026-10-04", 1000.4)
		_add(A, "Sales Invoice", "SI-0004", "2026-10-05", 1000)
		_add(B, "Purchase Invoice", "PI-4", "2026-10-05", 900, bill_no="SI-0004")
		_add(B, "Sales Invoice", "BS-1", "2026-10-05", 200)
		_add(A, "Purchase Invoice", "PI-A1", "2026-10-06", 200, bill_no="BS-1")
		_add(A, "Sales Invoice", "SI-0005", "2026-10-07", 50, is_cancelled=1)
		result = reconcile.reconcile(A, B)

		self.assertEqual(_status(result, "PI-1"), "matched")
		self.assertEqual(
			_status(result, "PI-2"), "matched", "the bill number is the invoice's trailing number"
		)
		self.assertEqual(_status(result, "PI-3"), "matched", "no bill number: same date and amount")
		self.assertEqual(_status(result, "PI-4"), "amount_differs")
		self.assertEqual(_status(result, "BS-1"), "date_differs")
		self.assertEqual(_status(result, "SI-0005"), [], "cancelled documents are left out")
		line = next(line for line in result["lines"] if (line["b"] or {}).get("name") == "BS-1")
		self.assertEqual(line["a"]["name"], "PI-A1", "B's sale is shown on B's side")

	def test_an_ambiguous_invoice_is_not_guessed(self):
		_add(A, "Sales Invoice", "X-1263", "2026-10-03", 300)
		_add(A, "Sales Invoice", "Y-1263", "2026-10-03", 300)
		_add(A, "Sales Invoice", "Z-21263", "2026-10-03", 300)
		_add(B, "Purchase Invoice", "PI-5", "2026-10-03", 300, bill_no="1263")
		result = reconcile.reconcile(A, B)
		self.assertEqual(_status(result, "PI-5"), "only_b")
		self.assertEqual(result["summary"]["invoice"]["only_a"], 3)

	def test_money_matches_across_payments_and_journals(self):
		_add(A, "Payment Entry", "PAY-A1", "2026-10-10", 5000, payment_type="Pay")
		_add(B, "Payment Entry", "PAY-B1", "2026-10-12", 5000, payment_type="Receive")
		_add(A, "Payment Entry", "PAY-A2", "2026-10-15", 700, payment_type="Receive")
		_add(B, "Journal Entry", "JV-B1", "2026-10-15", 700, party_type="Supplier")
		_add(A, "Journal Entry", "JV-A1", "2026-10-16", 100, party_type="Customer")
		_add(A, "Journal Entry", "JV-A1", "2026-10-16", -100, party_type="Supplier")
		_add(B, "Payment Entry", "PAY-B2", "2026-10-20", 999, payment_type="Pay")
		_add(A, "Payment Entry", "PAY-A3", "2026-10-25", 5000, payment_type="Pay")
		_add(B, "Payment Entry", "PAY-B3", "2026-11-05", 5000, payment_type="Receive")
		result = reconcile.reconcile(A, B)

		self.assertEqual(_status(result, "PAY-A1"), "matched", "two days apart is still the same transfer")
		self.assertEqual(
			_status(result, "PAY-A2"), "matched", "a receipt can be a journal entry on the other side"
		)
		self.assertEqual(_status(result, "JV-A1"), [], "a set-off between own accounts is not money")
		self.assertEqual(_status(result, "PAY-B2"), "only_b")
		self.assertEqual(_status(result, "PAY-A3"), "only_a", "eleven days apart is too far")
		self.assertEqual(_status(result, "PAY-B3"), "only_b")

	def test_balances_use_the_latest_figure_and_open_lines_explain_the_gap(self):
		_balance(A, "Customer", "2026-10-01", 5000)
		_balance(A, "Customer", "2026-10-31", 1000)
		_balance(B, "Supplier", "2026-10-31", -900)
		_add(A, "Sales Invoice", "SI-9", "2026-10-20", 100)
		result = reconcile.reconcile(A, B)
		self.assertEqual(result["balance"]["a"], 1000)
		self.assertEqual(result["balance"]["b"], -900)
		self.assertEqual(result["summary"]["balance_difference"], 100)
		self.assertEqual(result["summary"]["unexplained"], 0, "B has not booked SI-9: that is the whole gap")
		self.assertEqual(result["summary"]["open"], 1)
		self.assertIsNone(
			reconcile.reconcile(A, B, from_date="2026-10-15")["summary"]["unexplained"],
			"balances cover all of history, so a period cannot explain them",
		)

	def test_dashboard_lists_each_pair_once_with_chart_data(self):
		_add(A, "Sales Invoice", "SI-1", "2026-10-01", 100)
		_add(B, "Purchase Invoice", "PI-1", "2026-10-01", 100, bill_no="SI-1")
		_add(A, "Sales Invoice", "SI-OLD", "2025-01-15", 250)
		data = reconcile.dashboard()
		pairs = data["pairs"]
		self.assertEqual(len(pairs), 1)
		self.assertEqual((pairs[0]["a"]["name"], pairs[0]["b"]["name"]), ("Alpha Ltd", "Beta Ltd"))
		self.assertEqual(pairs[0]["summary"]["invoice"]["matched"], 1)
		self.assertEqual(
			data["months"],
			[
				{"month": "2025-01", "matched": 0, "differs": 0, "missing": 1},
				{"month": "2026-10", "matched": 1, "differs": 0, "missing": 0},
			],
		)
		self.assertEqual(data["ageing"][-1], {"bucket": "Over 180 days", "count": 1, "amount": 250})
		self.assertEqual(sum(b["count"] for b in data["ageing"]), 1, "only open lines have an age")
		self.assertIn("sites", data["sync"])

	def test_only_a_system_manager_can_read_it(self):
		self.assertRaises(frappe.ValidationError, reconcile.pair, A, A)
		# On v15, frappe.only_for lets everyone through while tests run.
		in_test, frappe.flags.in_test = frappe.flags.in_test, False
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, reconcile.pair, A, B)
			self.assertRaises(frappe.PermissionError, reconcile.dashboard)
		finally:
			frappe.set_user("Administrator")
			frappe.flags.in_test = in_test

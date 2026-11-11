"""Tests for related-party transactions: the register, syncing documents with related parties, and the
report."""

import frappe

from accounts_intelligence import reconcile, rpt
from accounts_intelligence.auth import server
from accounts_intelligence.sync import BALANCE, RELATED_PARTY, TRANSACTION
from accounts_intelligence.sync import server as sync_server
from accounts_intelligence.tests.test_auth import FrappeTestCase
from accounts_intelligence.tests.test_reconcile import A, B, two_companies

DIRECTOR = "AAAPD1234D"
FIRM = "AABFF5678F"
SITE_A = f"https://{A}.example.com"
SITE_B = f"https://{B}.example.com"


def _tx(entity, counterparty, doctype, name, posting_date, net, tax=0.0, **values):
	row = {
		"entity": entity,
		"counterparty": counterparty,
		"source_doctype": doctype,
		"source_name": name,
		"posting_date": posting_date,
		"party_type": "Customer" if doctype == "Sales Invoice" else "Supplier",
		"party": counterparty,
		"net_total": net,
		"tax_amount": tax,
		"grand_total": net + tax,
		"base_grand_total": net + tax,
		"source_modified": "2026-10-01 10:00:00",
		**values,
	}
	sync_server._upsert(TRANSACTION, [sync_server._transaction(row)], version="source_modified")


def _request(data, site):
	row = frappe._dict(name="no-row", site_url=site)
	return server.SignedRequest(row=row, data=data, public_key="", nonce="a" * 32)


class TestRelatedParties(FrappeTestCase):
	def setUp(self):
		two_companies()
		frappe.db.delete(RELATED_PARTY)
		self._register(DIRECTOR, "Director One", "Key managerial personnel")
		self._register(
			FIRM,
			"Director's Firm",
			"Entity controlled by key managerial personnel or a relative",
			related_to=A,
			related_from="2026-07-01",
		)

	def _register(self, pan, name, relationship, **values):
		return frappe.get_doc(
			{"doctype": RELATED_PARTY, "pan": pan, "party_name": name, "relationship": relationship, **values}
		).insert(ignore_permissions=True)

	def test_the_register_takes_a_pan_or_a_gstin_and_refuses_the_rest(self):
		doc = self._register("27aacpr1234k1z5", "Relative", "Relative of key managerial personnel")
		self.assertEqual(doc.name, "AACPR1234K", "the PAN is taken from the GSTIN")
		for pan, values in (
			("12345", {}),
			(A, {}),
			("AACPR9999K", {"related_from": "2026-05-01", "related_until": "2026-04-01"}),
		):
			self.assertRaises(
				frappe.ValidationError, self._register, pan, "Bad", "Other related party", **values
			)

	def test_each_site_watches_only_its_own_related_parties(self):
		watch_a = sync_server.config_handler(
			_request({"companies": [{"company": "Alpha Ltd", "pan": A}]}, SITE_A)
		)
		watch_b = sync_server.config_handler(
			_request({"companies": [{"company": "Beta Ltd", "pan": B}]}, SITE_B)
		)
		self.assertEqual(set(watch_a["watch"]), {A, B, DIRECTOR, FIRM})
		self.assertEqual(set(watch_b["watch"]), {A, B, DIRECTOR}, "the firm is related to Alpha only")
		self.assertEqual(watch_a["names"][DIRECTOR], "Director One")
		self.assertNotEqual(watch_a["version"], watch_b["version"])

	def test_sync_accepts_documents_with_a_related_party_of_the_sending_company_only(self):
		def row(entity, counterparty, name):
			return {
				"entity": entity,
				"counterparty": counterparty,
				"source_doctype": "Purchase Invoice",
				"source_name": name,
				"posting_date": "2026-08-01",
				"party_type": "Supplier",
				"party": "Firm",
				"grand_total": 100,
				"source_modified": "2026-08-01 10:00:00",
			}

		reply = sync_server.sync_handler(
			_request({"transactions": [row(A, FIRM, "PI-1"), row(A, "AAAPZ0000Z", "PI-2")]}, SITE_A)
		)
		self.assertEqual((reply["transactions"], reply["skipped_count"]), (1, 1), "an unknown PAN is refused")
		reply = sync_server.sync_handler(_request({"transactions": [row(B, FIRM, "PI-3")]}, SITE_B))
		self.assertEqual(reply["transactions"], 0, "the firm is not related to Beta")
		self.assertIn("related party", reply["skipped"][0]["reason"])

	def test_a_related_party_is_never_half_of_a_reconciliation_pair(self):
		_tx(A, DIRECTOR, "Sales Invoice", "SI-D", "2026-08-01", 1000)
		_tx(A, B, "Sales Invoice", "SI-B", "2026-08-01", 300)
		self.assertEqual(reconcile.pairs(), [(A, B)])

	def test_the_report_adds_up_each_partys_transactions_in_the_period(self):
		_tx(A, DIRECTOR, "Sales Invoice", "SI-1", "2026-08-01", 1000, tax=180)
		_tx(A, DIRECTOR, "Sales Invoice", "SI-RET", "2026-08-20", -200, tax=-36, is_return=1)
		_tx(A, DIRECTOR, "Payment Entry", "PE-1", "2026-09-01", 1180, payment_type="Receive")
		_tx(A, DIRECTOR, "Sales Invoice", "SI-X", "2026-08-02", 999, is_cancelled=1)
		_tx(A, FIRM, "Purchase Invoice", "PI-EARLY", "2026-06-15", 400)
		_tx(A, FIRM, "Purchase Invoice", "PI-1", "2026-08-10", 500, tax=90)
		_tx(B, DIRECTOR, "Sales Invoice", "SI-B1", "2026-08-05", 200)
		_tx(B, FIRM, "Purchase Invoice", "PI-B1", "2026-08-05", 700)
		_tx(A, B, "Sales Invoice", "SI-G", "2026-08-01", 300)
		_tx(A, DIRECTOR, "Sales Invoice", "SI-LATE", "2026-10-05", 5000)
		for as_of, balance in (("2026-08-31", 100), ("2026-10-15", 50)):
			sync_server._upsert(
				BALANCE,
				[
					sync_server._balance(
						{
							"entity": A,
							"counterparty": DIRECTOR,
							"party_type": "Customer",
							"party": DIRECTOR,
							"as_of": as_of,
							"balance": balance,
						}
					)
				],
			)

		data = rpt.report("2026-04-01", "2026-09-30")
		rows = {(r["company"]["pan"], r["party"]["pan"]): r for r in data["rows"]}
		self.assertEqual(
			[(r["company"]["pan"], r["party"]["pan"]) for r in data["rows"]],
			[(A, B), (A, DIRECTOR), (B, DIRECTOR), (A, FIRM)],
			"group companies first, then the register's relationships in their order",
		)
		director = rows[(A, DIRECTOR)]
		self.assertEqual(
			director["sales"], 800, "without tax, the return netted off, the cancelled one left out"
		)
		self.assertEqual(director["received"], 1180)
		self.assertEqual(director["documents"], 3)
		self.assertEqual(director["balance"], 100, "the balance on the period's last day")
		self.assertEqual(rows[(A, FIRM)]["purchases"], 500, "only from the day the firm became related")
		self.assertNotIn((B, FIRM), rows, "the firm is related to Alpha only")
		self.assertEqual(rows[(A, B)]["party"]["relationship"], rpt.GROUP)

		only_b = rpt.report("2026-04-01", "2026-09-30", company=B)["rows"]
		self.assertEqual([(r["company"]["pan"], r["party"]["pan"]) for r in only_b], [(B, DIRECTOR)])

	def test_the_period_defaults_to_the_financial_year(self):
		self.assertEqual(rpt.report(to_date="2026-02-10")["from_date"], "2025-04-01")
		self.assertEqual(rpt.report(to_date="2026-11-12")["from_date"], "2026-04-01")
		self.assertRaises(frappe.ValidationError, rpt.report, "2026-05-01", "2026-04-01")

	def test_only_a_system_manager_can_read_it(self):
		in_test, frappe.flags.in_test = frappe.flags.in_test, False
		frappe.set_user("Guest")
		try:
			self.assertRaises(frappe.PermissionError, rpt.report)
		finally:
			frappe.set_user("Administrator")
			frappe.flags.in_test = in_test

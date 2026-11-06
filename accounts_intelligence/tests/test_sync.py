"""Tests for data sync: the server's config and sync handlers, the client's collector, and full client
runs routed straight into the server handlers."""

import json
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime
from frappe.utils.password import remove_encrypted_password

from accounts_intelligence.auth import (
	ALLOWED_SITE,
	AUTH_LOG,
	SETTINGS,
	admin,
	keys,
	server,
	signing,
	transport,
)
from accounts_intelligence.sync import BALANCE, ENTITY, TRANSACTION, pan_of
from accounts_intelligence.sync import client as sync_client
from accounts_intelligence.sync import server as sync_server
from accounts_intelligence.tests.test_auth import (
	CLIENT,
	SERVER,
	FrappeTestCase,
	_reset_settings,
	_row,
	clear_counters,
)

OWN_PAN = "AAACA1111A"
OTHER_PAN = "AAACB2222B"
THIRD_PAN = "AAACC3333C"
OTHER_SITE = "https://other.example.com"
COMPANY = "AI Sync Test Co"
GROUP_CUSTOMER = "AI Sync Group Customer"
GROUP_SUPPLIER = "AI Sync Group Supplier"
OUTSIDER = "AI Sync Outsider"
OWN_BRANCH = "AI Sync Own Branch"
POST_SIGNED = "accounts_intelligence.auth.transport.post_signed"


def _request(data, site=CLIENT, row_name=None):
	row = frappe._dict(name=row_name or "no-row", site_url=site)
	return server.SignedRequest(row=row, data=data, public_key="", nonce="a" * 32)


def _register_other_company():
	frappe.get_doc(
		{"doctype": ENTITY, "pan": OTHER_PAN, "company_name": "Other Co", "site_url": OTHER_SITE}
	).insert(ignore_permissions=True)


def _clear_sync_tables():
	for doctype in (ENTITY, TRANSACTION, BALANCE):
		frappe.db.delete(doctype)


def _transaction(**overrides):
	return {
		"entity": OWN_PAN,
		"counterparty": OTHER_PAN,
		"source_doctype": "Sales Invoice",
		"source_name": "SINV-0001",
		"posting_date": "2026-10-01",
		"party_type": "Customer",
		"party": "Other Co",
		"party_name": "Other Co",
		"currency": "INR",
		"grand_total": 118000,
		"net_total": 100000,
		"tax_amount": 18000,
		"base_grand_total": 118000,
		"source_modified": "2026-10-01 10:00:00.000000",
		**overrides,
	}


class TestPAN(FrappeTestCase):
	def test_pan_comes_from_a_pan_or_a_gstin(self):
		self.assertEqual(pan_of(None, "27aaacb2222b1z5"), OTHER_PAN)
		self.assertEqual(pan_of(" aaacb2222b "), OTHER_PAN)
		self.assertEqual(pan_of("", None, "not a pan", "29AAACB2222B1Z5"), OTHER_PAN)
		self.assertIsNone(pan_of("", None, "12345", "27AAAC12222B1Z5"))


class TestSyncServer(FrappeTestCase):
	def setUp(self):
		_reset_settings(row_status="Active", row_key=keys.generate_keypair()[1])
		frappe.db.set_single_value(SETTINGS, {"sync_from": None, "sync_every": "15 minutes"})
		_clear_sync_tables()
		_register_other_company()
		self.config = sync_server.config_handler(
			_request({"companies": [{"company": "Alpha", "pan": OWN_PAN}]})
		)

	def _sync(self, **batch):
		return sync_server.sync_handler(_request(batch, row_name=_row().name))

	def test_config_registers_the_company_and_returns_the_watch_list(self):
		self.assertEqual(self.config["entities"], [OWN_PAN])
		self.assertEqual(self.config["watch"], [OWN_PAN, OTHER_PAN])
		self.assertEqual(frappe.db.get_value(ENTITY, OWN_PAN, "site_url"), CLIENT)

	def test_config_carries_the_sync_rules(self):
		self.assertEqual((self.config["sync_from"], self.config["sync_every"]), (None, 15))
		frappe.db.set_single_value(SETTINGS, {"sync_from": "2026-04-01", "sync_every": "6 hours"})
		reply = sync_server.config_handler(_request({"companies": []}))
		self.assertEqual((reply["sync_from"], reply["sync_every"]), ("2026-04-01", 360))

	def test_config_refuses_another_sites_pan_and_a_missing_pan(self):
		reply = sync_server.config_handler(
			_request(
				{"companies": [{"company": "Fake", "pan": OTHER_PAN}, {"company": "No PAN", "pan": "12"}]}
			)
		)
		self.assertEqual(reply["entities"], [])
		self.assertEqual(len(reply["refused"]), 2)
		self.assertEqual(frappe.db.get_value(ENTITY, OTHER_PAN, "site_url"), OTHER_SITE)
		self.assertTrue(frappe.db.exists(AUTH_LOG, {"event": "Sync config", "result": "Rejected"}))

	def test_resent_batch_is_stored_once(self):
		self._sync(transactions=[_transaction()])
		reply = self._sync(transactions=[_transaction()])
		self.assertEqual(reply["transactions"], 1)
		self.assertEqual(frappe.db.count(TRANSACTION), 1)
		row = frappe.get_all(TRANSACTION, fields=["entity", "counterparty", "grand_total", "posting_date"])[0]
		self.assertEqual((row.entity, row.counterparty, row.grand_total), (OWN_PAN, OTHER_PAN, 118000))

	def test_older_copy_never_overwrites_a_newer_one(self):
		self._sync(transactions=[_transaction(grand_total=200, source_modified="2026-10-02 10:00:00")])
		self._sync(transactions=[_transaction(grand_total=100, source_modified="2026-10-01 10:00:00")])
		self.assertEqual(frappe.db.get_value(TRANSACTION, {}, "grand_total"), 200)
		self._sync(transactions=[_transaction(grand_total=300, is_cancelled=1, source_modified="2026-10-03")])
		row = frappe.db.get_value(TRANSACTION, {}, ["grand_total", "is_cancelled"], as_dict=True)
		self.assertEqual((row.grand_total, row.is_cancelled), (300, 1))

	def test_bad_rows_are_skipped_and_reported(self):
		reply = self._sync(
			transactions=[
				_transaction(),
				_transaction(source_name="SINV-2", entity=OTHER_PAN, counterparty=OWN_PAN),
				_transaction(source_name="SINV-3", counterparty=THIRD_PAN),
				_transaction(source_name="SINV-4", posting_date="01/10/2026"),
				_transaction(source_name="SINV-5", source_doctype="Sales Order"),
				_transaction(source_name="SINV-6", counterparty=OWN_PAN),
				"not a row",
			]
		)
		self.assertEqual(reply["transactions"], 1)
		self.assertEqual(reply["skipped_count"], 6)
		reasons = " ".join(s["reason"] for s in reply["skipped"])
		self.assertIn("not registered to this site", reasons)
		self.assertIn("not a group company", reasons)
		self.assertIn("its own counterparty", reasons)

	def test_oversized_batch_is_rejected(self):
		with self.assertRaises(server.Rejected):
			self._sync(transactions=[_transaction(source_name=f"S-{i}") for i in range(501)])

	def test_site_start_date_overrides_the_group_date(self):
		frappe.db.set_single_value(SETTINGS, "sync_from", "2026-04-01")
		self.assertEqual(sync_server.site_rules(_row().name)["sync_from"], "2026-04-01")
		frappe.db.set_value(ALLOWED_SITE, _row().name, "sync_from", "2026-06-01")
		self.assertEqual(sync_server.site_rules(_row().name)["sync_from"], "2026-06-01")

	def test_resync_is_requested_for_connected_sites_only(self):
		self.assertIsNone(sync_server.site_rules(_row().name)["resync"])
		admin.server_resync([_row().name])
		self.assertIsNotNone(sync_server.site_rules(_row().name)["resync"])
		admin.server_revoke([_row().name])
		self.assertRaises(frappe.ValidationError, admin.server_resync, [_row().name])

	def test_daily_check_compares_monthly_totals(self):
		self._sync(transactions=[_transaction()])
		key = f"Sales Invoice|{OWN_PAN}|2026-10"
		reply = self._sync(check={key: [1, 118000]})
		self.assertEqual(reply["differences"], [])
		self.assertIn("all 1 months match", _row().last_check)

		reply = self._sync(check={key: [1, 118000], f"Sales Invoice|{OWN_PAN}|2026-09": [2, 50]})
		self.assertEqual([d["key"] for d in reply["differences"]], [f"Sales Invoice|{OWN_PAN}|2026-09"])
		self.assertEqual(reply["differences"][0]["server"], [0, 0.0])
		self.assertIn("1 differ", _row().last_check)
		self.assertRaises(server.Rejected, self._sync, check={key: "bad"})

	def test_balances_are_stored_and_the_site_row_records_the_sync(self):
		balance = {
			"entity": OWN_PAN,
			"counterparty": OTHER_PAN,
			"party_type": "Customer",
			"party": "Other Co",
		}
		self._sync(balances=[{**balance, "as_of": "2026-11-01", "balance": 300}])
		self._sync(balances=[{**balance, "as_of": "2026-11-01", "balance": 250}])
		self._sync(balances=[{**balance, "as_of": "2026-11-02", "balance": 100}])
		rows = frappe.get_all(BALANCE, fields=["as_of", "balance"], order_by="as_of")
		self.assertEqual([r.balance for r in rows], [250, 100])
		self.assertEqual(_row().last_sync_rows, 1)
		self.assertIsNotNone(_row().last_sync_on)
		self._sync(check={})
		self.assertEqual(_row().last_sync_rows, 1, "a call without rows keeps the last batch size")


class TestSyncEndpoint(FrappeTestCase):
	def setUp(self):
		self.client_private, self.client_public = keys.generate_keypair()
		_reset_settings(row_status="Active", row_key=self.client_public)
		_clear_sync_tables()
		_register_other_company()
		sync_server.config_handler(_request({"companies": [{"company": "Alpha", "pan": OWN_PAN}]}))
		clear_counters()

	def tearDown(self):
		clear_counters()
		frappe.local.request = None

	def test_signed_batch_is_stored_and_the_reply_is_signed(self):
		from werkzeug.test import EnvironBuilder

		path = "/api/method/accounts_intelligence.api.sync"
		body = json.dumps({"transactions": [_transaction()]}).encode()
		headers = signing.sign_request(
			self.client_private, self.client_public, "POST", path, SERVER, CLIENT, body
		)
		frappe.local.request = EnvironBuilder(
			path=path, method="POST", headers={**headers, "Content-Type": "application/json"}, data=body
		).get_request()
		frappe.local.response = frappe._dict()

		envelope = server.handle("Sync", sync_server.sync_handler)
		server_public = frappe.db.get_value(SETTINGS, SETTINGS, "public_key")
		reply = signing.open_response(server_public, headers[signing.HEADER_NONCE], envelope)
		self.assertEqual((reply["transactions"], reply["for_site"]), (1, CLIENT))
		self.assertEqual(frappe.db.count(TRANSACTION), 1)


def _insert(doctype, **values):
	doc = frappe.get_doc({"doctype": doctype, **values})
	doc.db_insert()
	return doc


class TestSyncClient(FrappeTestCase):
	"""A client with one company, one group customer, one group supplier and an outside customer."""

	def setUp(self):
		self._client_settings()
		_clear_sync_tables()
		_register_other_company()
		self.now = now_datetime()
		frappe.db.delete("Company", {"name": COMPANY})
		_insert(
			"Company", name=COMPANY, company_name=COMPANY, abbr="AIST", default_currency="INR", pan=OWN_PAN
		)
		for party_type, name, values in (
			("Customer", GROUP_CUSTOMER, {"customer_name": GROUP_CUSTOMER, "gstin": f"27{OTHER_PAN}1Z5"}),
			("Customer", OUTSIDER, {"customer_name": OUTSIDER, "pan": "AAACZ9999Z"}),
			("Supplier", GROUP_SUPPLIER, {"supplier_name": GROUP_SUPPLIER, "tax_id": OTHER_PAN}),
			# The company itself under another GSTIN, as GGIL and GGSPL keep it: never a group company.
			("Customer", OWN_BRANCH, {"customer_name": COMPANY, "gstin": f"06{OWN_PAN}1Z5"}),
		):
			frappe.db.delete(party_type, {"name": name})
			_insert(party_type, name=name, **values)

		self._document("Sales Invoice", "AI-SINV-1", customer=GROUP_CUSTOMER, grand_total=118, net_total=100)
		self._document("Sales Invoice", "AI-SINV-2", customer=OUTSIDER, grand_total=50)
		self._document("Sales Invoice", "AI-SINV-OWN", customer=OWN_BRANCH, grand_total=999)
		self._document("Sales Invoice", "AI-SINV-3", customer=GROUP_CUSTOMER, docstatus=0)
		self._document(
			"Purchase Invoice", "AI-PINV-1", supplier=GROUP_SUPPLIER, bill_no="B-1", grand_total=70
		)
		self._document(
			"Payment Entry",
			"AI-PE-1",
			payment_type="Receive",
			party_type="Customer",
			party=GROUP_CUSTOMER,
			received_amount=118,
			base_received_amount=118,
			paid_to_account_currency="INR",
		)
		# Records persist between tests of one class, so child rows and ledger rows are cleared first.
		frappe.db.delete("Journal Entry Account", {"parent": "AI-JV-1"})
		frappe.db.delete("GL Entry", {"company": COMPANY})
		self._document("Journal Entry", "AI-JV-1")
		for party, debit, credit in ((GROUP_CUSTOMER, 40, 0), (GROUP_CUSTOMER, 0, 10), (OUTSIDER, 0, 30)):
			_insert(
				"Journal Entry Account",
				name=frappe.generate_hash(length=10),
				parent="AI-JV-1",
				parenttype="Journal Entry",
				parentfield="accounts",
				party_type="Customer",
				party=party,
				debit=debit,
				credit=credit,
			)
		for debit, credit in ((500, 0), (0, 200)):
			_insert(
				"GL Entry",
				name=frappe.generate_hash(length=10),
				company=COMPANY,
				party_type="Customer",
				party=GROUP_CUSTOMER,
				debit=debit,
				credit=credit,
				is_cancelled=0,
				posting_date="2026-10-01",
			)

	def tearDown(self):
		frappe.cache.delete(frappe.cache.make_key(sync_client.LOCK))

	def _client_settings(self):
		server_public = keys.generate_keypair()[1]
		frappe.db.delete(ALLOWED_SITE, {"parent": SETTINGS})
		frappe.db.set_single_value(
			SETTINGS,
			{
				"mode": "Client",
				"site_url": CLIENT,
				"server_url": SERVER,
				"allow_http_for_testing": 0,
				"server_public_key": server_public,
				"server_fingerprint": keys.fingerprint(server_public),
				"connection_status": "Connected",
				"public_key": None,
				"key_fingerprint": None,
				"pending_public_key": None,
			},
		)
		remove_encrypted_password(SETTINGS, SETTINGS, "pending_private_key")
		frappe.get_single(SETTINGS).save()
		frappe.db.set_single_value(
			SETTINGS,
			{
				"sync_enabled": 1,
				"sync_state": None,
				"last_sync_result": None,
				"last_sync_on": None,
				"sync_from": None,
				"sync_every": "15 minutes",
			},
		)

	def _document(self, doctype, name, docstatus=1, **values):
		frappe.db.delete(doctype, {"name": name})
		self.now = add_to_date(self.now, seconds=1)
		return _insert(
			doctype,
			name=name,
			company=COMPANY,
			posting_date="2026-10-01",
			docstatus=docstatus,
			modified=self.now,
			**values,
		)

	def _parties(self):
		return sync_client.watched_parties({OTHER_PAN})

	def test_group_companies_are_found_by_gstin_or_tax_id_only(self):
		parties = self._parties()
		self.assertEqual(parties[("Customer", GROUP_CUSTOMER)][0], OTHER_PAN)
		self.assertEqual(parties[("Supplier", GROUP_SUPPLIER)][0], OTHER_PAN)
		self.assertNotIn(("Customer", OUTSIDER), parties)

	def test_a_party_is_found_by_the_gstin_on_its_address(self):
		frappe.db.delete("Customer", {"name": "AI Sync Address Only"})
		_insert("Customer", name="AI Sync Address Only", customer_name="AI Sync Address Only")
		address = _insert(
			"Address",
			name=frappe.generate_hash(length=10),
			address_line1="x",
			city="x",
			gstin=f"09{OTHER_PAN}1Z5",
		)
		_insert(
			"Dynamic Link",
			name=frappe.generate_hash(length=10),
			parent=address.name,
			parenttype="Address",
			parentfield="links",
			link_doctype="Customer",
			link_name="AI Sync Address Only",
		)
		self.assertEqual(self._parties()[("Customer", "AI Sync Address Only")][0], OTHER_PAN)

	def test_the_site_company_is_the_default_company(self):
		with patch("frappe.defaults.get_global_default", return_value=COMPANY):
			self.assertEqual(sync_client.own_companies(), ({COMPANY: OWN_PAN}, []))
		# With no default and several companies, nothing is sent and the admin is told why.
		with patch("frappe.defaults.get_global_default", return_value=None):
			companies, notes = sync_client.own_companies()
		self.assertEqual(companies, {})
		self.assertIn("Default Company", notes[0])

	def test_party_named_like_a_group_company_without_its_pan_is_reported(self):
		frappe.db.delete("Customer", {"name": "AI Sync Beta"})
		_insert("Customer", name="AI Sync Beta", customer_name="M/S Beta Spices Pvt. Ltd.")
		notes = sync_client.unmatched_names(
			{OTHER_PAN: "Beta Spices Private Limited"}, {OTHER_PAN}, self._parties()
		)
		self.assertEqual(len(notes), 1)
		self.assertIn(
			"Customer AI Sync Beta is named like group company Beta Spices Private Limited", notes[0]
		)
		self.assertEqual(
			sync_client.unmatched_names({OTHER_PAN: "Beta Spices Private Limited"}, set(), {}), []
		)

	def test_collect_sends_only_submitted_documents_with_group_companies(self):
		companies, parties = {COMPANY: OWN_PAN}, self._parties()
		rows, watermark, more = sync_client.collect("Sales Invoice", companies, parties)
		self.assertEqual([r["source_name"] for r in rows], ["AI-SINV-1"])
		self.assertFalse(more)
		self.assertEqual((rows[0]["entity"], rows[0]["counterparty"]), (OWN_PAN, OTHER_PAN))
		self.assertEqual(sync_client.collect("Sales Invoice", companies, parties, watermark)[0], [])

		self.assertEqual(sync_client.collect("Purchase Invoice", companies, parties)[0][0]["bill_no"], "B-1")
		payment = sync_client.collect("Payment Entry", companies, parties)[0][0]
		self.assertEqual((payment["payment_type"], payment["grand_total"]), ("Receive", 118))
		journal = sync_client.collect("Journal Entry", companies, parties)[0]
		self.assertEqual(
			[(r["party"], r["grand_total"], r["currency"]) for r in journal], [(GROUP_CUSTOMER, 30, "INR")]
		)

	def _run_against_server(self, fail_on=None):
		"""Run the client with every call answered by the server handlers on this same site."""

		def fake_post(settings, method, data, nonce=None, key_pair=None):
			if method == fail_on:
				raise transport.TransportError("server unreachable")
			handler = {"sync_config": sync_server.config_handler, "sync": sync_server.sync_handler}[method]
			return {**handler(_request(json.loads(json.dumps(data)))), "for_site": CLIENT}

		with (
			patch(POST_SIGNED, side_effect=fake_post),
			patch.object(sync_client, "own_companies", return_value=({COMPANY: OWN_PAN}, [])),
		):
			return sync_client.run()

	def _state(self):
		return json.loads(frappe.db.get_single_value(SETTINGS, "sync_state"))

	def test_full_run_then_only_changes(self):
		self.assertEqual(self._run_against_server(), "Sent 4 documents and 1 balances.")
		self.assertEqual(frappe.db.count(TRANSACTION), 4)
		self.assertEqual(frappe.db.get_value(BALANCE, {}, "balance"), 300)
		self.assertEqual(self._run_against_server(), "Up to date.")

		self._document("Sales Invoice", "AI-SINV-1", customer=GROUP_CUSTOMER, grand_total=236, docstatus=2)
		self.assertEqual(self._run_against_server(), "Sent 1 documents and 0 balances.")
		row = frappe.db.get_value(
			TRANSACTION, {"source_name": "AI-SINV-1"}, ["grand_total", "is_cancelled"], as_dict=True
		)
		self.assertEqual((row.grand_total, row.is_cancelled), (236, 1))

	def test_failed_send_keeps_the_watermark(self):
		result = self._run_against_server(fail_on="sync")
		self.assertEqual(result, "server unreachable")
		self.assertEqual(self._state()["watermarks"], {})
		self.assertEqual(self._run_against_server(), "Sent 4 documents and 1 balances.")

	def test_new_group_company_sends_everything_again(self):
		self._run_against_server()
		frappe.get_doc(
			{
				"doctype": ENTITY,
				"pan": THIRD_PAN,
				"company_name": "Third Co",
				"site_url": "https://third.example.com",
			}
		).insert(ignore_permissions=True)
		self.assertEqual(self._run_against_server(), "Sent 4 documents and 1 balances.")
		self.assertEqual(frappe.db.count(TRANSACTION), 4)

	def test_sync_does_nothing_when_switched_off(self):
		frappe.db.set_single_value(SETTINGS, "sync_enabled", 0)
		with patch(POST_SIGNED) as post:
			self.assertIsNone(sync_client.run())
		post.assert_not_called()

	def test_documents_before_the_start_date_are_not_sent(self):
		companies, parties = {COMPANY: OWN_PAN}, self._parties()
		self.assertEqual(
			sync_client.collect("Sales Invoice", companies, parties, sync_from="2026-10-02")[0], []
		)
		self.assertEqual(
			len(sync_client.collect("Sales Invoice", companies, parties, sync_from="2026-10-01")[0]), 1
		)

	def test_start_date_comes_from_the_server_and_moving_it_earlier_resends(self):
		frappe.db.set_single_value(SETTINGS, {"sync_from": "2026-10-02", "sync_every": "1 hour"})
		self.assertEqual(self._run_against_server(), "Sent 0 documents and 1 balances.")
		self.assertEqual((self._state()["sync_from"], self._state()["sync_every"]), ("2026-10-02", 60))

		frappe.db.set_single_value(SETTINGS, "sync_from", "2026-10-03")
		self.assertEqual(self._run_against_server(), "Up to date.")

		frappe.db.set_single_value(SETTINGS, "sync_from", None)
		self.assertEqual(self._run_against_server(), "Sent 4 documents and 1 balances.")

	def test_schedule_waits_for_the_interval(self):
		state = {"sync_every": 60}
		frappe.db.set_single_value(SETTINGS, {"last_sync_on": None, "sync_state": json.dumps(state)})
		self.assertTrue(sync_client._due(), "never synced: run now")
		frappe.db.set_single_value(SETTINGS, "last_sync_on", add_to_date(now_datetime(), minutes=-30))
		self.assertFalse(sync_client._due())
		frappe.db.set_single_value(SETTINGS, "last_sync_on", add_to_date(now_datetime(), minutes=-60))
		self.assertTrue(sync_client._due())

	def _check_again(self):
		state = self._state()
		state["checked_on"] = "2000-01-01"
		frappe.db.set_single_value(SETTINGS, "sync_state", json.dumps(state))

	def test_daily_check_resends_a_month_the_server_is_missing(self):
		self._run_against_server()
		frappe.db.delete(TRANSACTION, {"source_name": "AI-SINV-1"})
		self._check_again()
		result = self._run_against_server()
		self.assertIn("daily check sent 1 Sales Invoice documents of 2026-10 again", result)
		self.assertEqual(frappe.db.count(TRANSACTION), 4)
		self.assertEqual(self._run_against_server(), "Up to date.", "the check runs once a day")

	def test_daily_check_reports_documents_deleted_here(self):
		self._run_against_server()
		frappe.db.delete("Sales Invoice", {"name": "AI-SINV-1"})
		self._check_again()
		result = self._run_against_server()
		self.assertIn("Sales Invoice 2026-10: the server has 1 documents, this site 0", result)

	def test_resync_request_sends_everything_again(self):
		self._run_against_server()
		rules = {"sync_from": None, "sync_every": 15, "resync": "2026-11-05 10:00:00"}
		with patch.object(sync_server, "site_rules", return_value=rules):
			self.assertEqual(self._run_against_server(), "Sent 4 documents and 1 balances.")
			self.assertEqual(self._run_against_server(), "Up to date.", "the same request is not repeated")

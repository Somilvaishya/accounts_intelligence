import frappe
from frappe.model.document import Document


class AccountsIntelligenceTransaction(Document):
	pass


def on_doctype_update():
	# Matching looks up one company's documents with another by date.
	frappe.db.add_index("Accounts Intelligence Transaction", ["counterparty", "posting_date"])

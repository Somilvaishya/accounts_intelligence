import frappe
from frappe.model.document import Document


class AccountsIntelligenceBalance(Document):
	pass


def on_doctype_update():
	# A balance on a date is the latest row for the pair on or before that date.
	frappe.db.add_index("Accounts Intelligence Balance", ["entity", "counterparty", "as_of"])

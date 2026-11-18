import frappe
from frappe.model.document import Document


class AccountsIntelligenceMatch(Document):
	pass


def on_doctype_update():
	# The reconciliation reads the matches of one company pair at a time.
	frappe.db.add_index("Accounts Intelligence Match", ["pair_a", "pair_b"])

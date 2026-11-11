import frappe
from frappe import _
from frappe.model.document import Document


class AccountsIntelligenceOpenItem(Document):
	def validate(self):
		if self.status == "Explained" and not self.reason:
			frappe.throw(_("Choose a reason to mark the line explained."))


def on_doctype_update():
	# The reconciliation reads the reviews of one company pair at a time.
	frappe.db.add_index("Accounts Intelligence Open Item", ["pair_a", "pair_b"])

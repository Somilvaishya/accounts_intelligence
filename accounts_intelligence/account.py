"""Is Related Party on an ERPNext Account.

Ticking it with a PAN tells the company site that entries on this ledger are dealings with that party,
even when no customer or supplier is on them: a director's loan account, rent or remuneration booked to
an account. If the PAN is a group company or in the group's related-party register, the site sends every
voucher on the account as an Account Entry (see sync/client.py).
"""

import frappe
from frappe import _
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from accounts_intelligence.sync import pan_of

FLAG = "ai_is_related_party"
PAN = "ai_related_party_pan"

FIELDS = {
	"Account": [
		{
			"fieldname": FLAG,
			"fieldtype": "Check",
			"label": "Is Related Party",
			"insert_after": "account_type",
			"description": "Entries on this account are dealings with a related party or a group company.",
		},
		{
			"fieldname": PAN,
			"fieldtype": "Data",
			"label": "Related Party PAN",
			"insert_after": FLAG,
			"depends_on": f"eval:doc.{FLAG}",
			"mandatory_depends_on": f"eval:doc.{FLAG}",
			"description": "The PAN of that party, or its GSTIN. Its entries reach the group server only when "
			"the PAN is a group company or in the group's related-party register.",
		},
	]
}


def setup():
	"""Add the two fields to Account. Runs after install and every migrate, so it is safe to repeat."""
	create_custom_fields(FIELDS, update=True)


def remove():
	"""Take the fields off Account when the app is uninstalled."""
	for doctype, fields in FIELDS.items():
		for field in fields:
			frappe.db.delete("Custom Field", {"dt": doctype, "fieldname": field["fieldname"]})
	frappe.clear_cache(doctype="Account")


def validate(doc, method=None):
	"""Keep the PAN clean: required and normalised when ticked, cleared when not."""
	if not doc.get(FLAG):
		doc.set(PAN, None)
		return
	if doc.is_group:
		frappe.throw(
			_("Tick Is Related Party on a ledger account: a group account has no entries of its own.")
		)
	pan = pan_of(doc.get(PAN))
	if not pan:
		frappe.throw(_("Related Party PAN: {0} is not a PAN or a GSTIN.").format(doc.get(PAN) or ""))
	doc.set(PAN, pan)

import frappe
from frappe import _
from frappe.client import get_password as frappe_get_password

from accounts_intelligence.auth import SETTINGS

# A site's private keys sign as that site. They are read only by this app's own code, never over HTTP.
UNREADABLE = {SETTINGS: {"private_key", "pending_private_key"}}


@frappe.whitelist()
def get_password(doctype: str, name: str | int, fieldname: str):
	"""Frappe's frappe.client.get_password, except that it refuses this app's private keys.

	Without this, any System Manager, or any API key with that role, could export the key and sign
	requests as this site from anywhere.
	"""
	if fieldname in UNREADABLE.get(doctype, ()):
		frappe.throw(_("This site's private key cannot be read through the API."), frappe.PermissionError)
	return frappe_get_password(doctype, name, fieldname)

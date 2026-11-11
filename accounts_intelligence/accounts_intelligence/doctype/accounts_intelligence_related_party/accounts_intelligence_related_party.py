import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate

from accounts_intelligence.sync import ENTITY, pan_of


class AccountsIntelligenceRelatedParty(Document):
	def autoname(self):
		self.pan = self._pan()
		self.name = self.pan

	def validate(self):
		self.pan = self._pan()
		if frappe.db.exists(ENTITY, self.pan):
			frappe.throw(
				_("{0} is a group company. Group companies are related to each other already.").format(
					self.pan
				)
			)
		if (
			self.related_from
			and self.related_until
			and getdate(self.related_from) > getdate(self.related_until)
		):
			frappe.throw(_("Related From must be on or before Related Until."))

	def _pan(self) -> str:
		pan = pan_of(self.pan)
		if not pan:
			frappe.throw(_("{0} is not a PAN or a GSTIN.").format(self.pan))
		return pan

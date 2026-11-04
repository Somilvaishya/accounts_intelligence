import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import escape_html, now_datetime

from accounts_intelligence.auth import audit, keys
from accounts_intelligence.auth.urls import InvalidURL, canonical_url, http_allowed

# Written only by the key setup and the connection actions, never by a form save.
MANAGED_FIELDS = (
	"public_key",
	"key_fingerprint",
	"key_created_on",
	"private_key",
	"pending_public_key",
	"pending_private_key",
	"server_public_key",
	"server_fingerprint",
	"connection_status",
	"last_connected_on",
	"last_error",
)
MANAGED_ROW_FIELDS = (
	"status",
	"public_key",
	"fingerprint",
	"requested_on",
	"approved_on",
	"approved_by",
	"last_seen",
	"key_rotated_on",
)


class AccountsIntelligenceSettings(Document):
	def validate(self):
		before = self.get_doc_before_save()
		self._keep_managed_fields(before)
		self._check_mode_change(before)
		self._check_plain_http(before)
		self._clean_urls(before)
		self._check_site_url_change(before)
		self._clean_allowed_sites(before)
		if self.mode and not self.public_key:
			self._create_identity()

	def on_update(self):
		if self.flags.identity_created:
			audit.log("Key created", "Accepted", site_url=self.site_url, fingerprint=self.key_fingerprint)
		for row in self.flags.removed_sites or ():
			audit.log(
				"Site removed",
				"Accepted",
				site_url=row.site_url,
				fingerprint=row.fingerprint,
				reason=f"status was {row.status}",
			)

	def _keep_managed_fields(self, before):
		for field in MANAGED_FIELDS:
			self.set(field, before.get(field) if before else None)
		if not self.connection_status:
			self.connection_status = "Not connected"

	def _check_mode_change(self, before):
		if not before or not before.mode or before.mode == self.mode:
			return
		if _has_connections(before):
			frappe.throw(
				_(
					"This site still has connections as a {0}. Reset them before changing the mode: "
					"Reset connection on a client, or reset every allowed site on a server."
				).format(before.mode)
			)

	def _check_plain_http(self, before):
		# List every http:// address in one message, so turning plain HTTP off takes one round of edits
		# instead of one failed save per address.
		if http_allowed(self.allow_http_for_testing):
			return
		found = []
		if _is_http(self.site_url):
			found.append(_("This Site's URL: {0}").format(self.site_url))
		if _is_http(self.server_url):
			found.append(_("Server URL: {0}").format(self.server_url))
		for row in self.allowed_sites:
			if _is_http(row.site_url):
				found.append(_("Allowed site in row {0}: {1}").format(row.idx, row.site_url))
		if not found:
			return
		if self.allow_http_for_testing:
			intro = _(
				"Allow Plain HTTP is on, but this bench is not in developer mode, so http:// is refused."
			)
		else:
			intro = _("Allow Plain HTTP is off, but these addresses still use http://.")
		steps = [_("Change each address to its https:// form, then save.")]
		if before and _has_connections(before):
			steps.insert(
				0,
				_(
					"This site has connections, so its URL cannot change yet. Reset them first: Reset Selected "
					"on each allowed site, or Reset Connection on a client."
				),
			)
		steps.append(_("Remove allowed sites that have no https:// address, such as local test sites."))
		items = "".join(f"<li>{escape_html(line)}</li>" for line in found)
		frappe.throw(
			f"<p>{escape_html(intro)}</p><ul>{items}</ul><p>{'<br>'.join(escape_html(s) for s in steps)}</p>",
			title=_("Plain HTTP is not allowed"),
		)

	def _clean_urls(self, before):
		if self.site_url or self.mode:
			self.site_url = self._canonical(
				self.site_url or frappe.conf.get("host_name"), _("This site's URL")
			)
		if self.mode == "Client" or self.server_url:
			self.server_url = self._canonical(self.server_url, _("Server URL"))
			if self.server_url == self.site_url:
				frappe.throw(_("The server URL cannot be this site's own URL."))
		if before and before.server_url and before.server_url != self.server_url:
			# A different server is a new connection: forget the old server's key.
			self.server_public_key = None
			self.server_fingerprint = None
			self.connection_status = "Not connected"
			self.last_connected_on = None
			self.last_error = None

	def _check_site_url_change(self, before):
		# The site URL is signed into every request, so changing it while connected silently breaks the
		# connection: a server's clients would all be refused, and a client would be unknown to its server.
		if not before or not before.site_url or before.site_url == self.site_url:
			return
		if _has_connections(before):
			frappe.throw(
				_(
					"This site's URL cannot change while it has connections. Reset them first, change the "
					"URL, then connect again. On a server, also update the URL on every client."
				)
			)

	def _clean_allowed_sites(self, before):
		previous = {row.name: row for row in (before.allowed_sites if before else [])}
		seen = set()
		for row in self.allowed_sites:
			row.site_url = self._canonical(row.site_url, _("Allowed site in row {0}").format(row.idx))
			if row.site_url == self.site_url:
				frappe.throw(_("Row {0}: a site cannot allow itself.").format(row.idx))
			if row.site_url in seen:
				frappe.throw(_("Row {0}: {1} is already on the list.").format(row.idx, row.site_url))
			seen.add(row.site_url)

			old = previous.get(row.name)
			if old and old.site_url == row.site_url:
				for field in MANAGED_ROW_FIELDS:
					row.set(field, old.get(field))
			else:
				# A new row, or a changed address, starts over and must connect and be approved again.
				for field in MANAGED_ROW_FIELDS:
					row.set(field, None)
				row.status = "Pending"

		# A deleted row, or a row whose address changed, takes that site off the list.
		kept = {row.name: row.site_url for row in self.allowed_sites}
		self.flags.removed_sites = [old for name, old in previous.items() if kept.get(name) != old.site_url]

	def _create_identity(self):
		private_key, public_key = keys.generate_keypair()
		self.private_key = private_key
		self.public_key = public_key
		self.key_fingerprint = keys.fingerprint(public_key)
		self.key_created_on = now_datetime()
		self.flags.identity_created = True

	def _canonical(self, value, label):
		try:
			return canonical_url(value, allow_http=self.allow_http_for_testing)
		except InvalidURL as e:
			frappe.throw(_("{0}: {1}").format(label, e))


def _is_http(value) -> bool:
	return (value or "").strip().lower().startswith("http://")


def _has_connections(doc) -> bool:
	if doc.server_public_key or (doc.connection_status or "Not connected") != "Not connected":
		return True
	return any(row.public_key or row.status != "Pending" for row in doc.allowed_sites)

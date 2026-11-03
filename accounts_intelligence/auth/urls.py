"""Site addresses are compared as exact strings, so every address is reduced to one canonical form first."""

import re
from urllib.parse import urlsplit

import frappe
from frappe import _

_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
_HOST_RE = re.compile(rf"^{_LABEL}(?:\.{_LABEL})*$")


class InvalidURL(ValueError):
	pass


def http_allowed(setting) -> bool:
	"""Plain HTTP needs both the testing switch in settings and a bench in developer mode."""
	return bool(setting) and bool(frappe.conf.developer_mode)


def canonical_url(value: str, allow_http=False) -> str:
	"""Reduce a site address to scheme://host[:port] in lower case, with the default port dropped."""
	raw = (value or "").strip()
	if not raw:
		raise InvalidURL(_("An address is required"))
	parts = urlsplit(raw)
	scheme = parts.scheme.lower()
	if scheme not in ("https", "http"):
		raise InvalidURL(_("{0} must start with https://").format(raw))
	if scheme == "http" and not http_allowed(allow_http):
		raise InvalidURL(
			_("{0} uses plain http://. Only https:// is allowed outside local testing.").format(raw)
		)
	if parts.username or parts.password or parts.query or parts.fragment or parts.path not in ("", "/"):
		raise InvalidURL(_("{0} must be only the site address, like https://erp.example.com").format(raw))
	host = (parts.hostname or "").lower()
	if len(host) > 253 or not _HOST_RE.match(host):
		raise InvalidURL(_("{0} does not have a valid host name").format(raw))
	try:
		port = parts.port
	except ValueError:
		raise InvalidURL(_("{0} has an invalid port").format(raw))
	default_port = 443 if scheme == "https" else 80
	netloc = host if port in (None, default_port) else f"{host}:{port}"
	return f"{scheme}://{netloc}"

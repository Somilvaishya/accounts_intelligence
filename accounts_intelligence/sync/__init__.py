"""Data sync: company sites send accounting document headers to the group server.

Only documents whose party is another group company are sent (the watch list), and only header fields:
no line items, no attachments. Group companies are identified by PAN, which every GSTIN of a company
contains, so one PAN covers all of a company's state registrations.
"""

import hashlib
import re
from datetime import date

ENTITY = "Accounts Intelligence Entity"
TRANSACTION = "Accounts Intelligence Transaction"
BALANCE = "Accounts Intelligence Balance"
RELATED_PARTY = "Accounts Intelligence Related Party"

SOURCES = ("Sales Invoice", "Purchase Invoice", "Payment Entry", "Journal Entry")
PARTY_TYPES = ("Customer", "Supplier")
MAX_ROWS = 500
# The "Sync Every" options on the server, in minutes. Clients check every 5 minutes whether a run is due.
SYNC_EVERY = {"5 minutes": 5, "15 minutes": 15, "30 minutes": 30, "1 hour": 60, "6 hours": 360, "1 day": 1440}
DEFAULT_SYNC_EVERY = 15

PAN_RE = re.compile(r"^[A-Z]{5}[0-9]{4}[A-Z]$")


def pan_of(*values) -> str | None:
	"""The first PAN found in the values, each of which may be a PAN or a GSTIN."""
	for value in values:
		value = str(value or "").strip().upper()
		if len(value) == 15:
			value = value[2:12]
		if PAN_RE.match(value):
			return value
	return None


def iso_date(value) -> str | None:
	"""A date as YYYY-MM-DD, or None when empty. An empty Date in a Single reads back as 0001-01-01 on v16."""
	try:
		parsed = date.fromisoformat(str(value)[:10]) if value else None
	except ValueError:
		return None
	return parsed.isoformat() if parsed and parsed.year > 1 else None


def row_key(*parts) -> str:
	"""A fixed-length name for a synced row, so a resent row lands on the same record."""
	return hashlib.sha256("\x1f".join(str(p) for p in parts).encode()).hexdigest()[:40]

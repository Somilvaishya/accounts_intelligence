"""The assistant: questions about the group's reconciliation, answered from it.

The AI model comes from the AI Interface app: its provider, model, budget, rate limit and call log. That app
is needed for the assistant only; the rest of Accounts Intelligence works without it. A question costs at
most two calls:

1. plan: the model picks one tool from TOOLS and its arguments, as JSON;
2. answer: our code runs the tool on the reconciled data, and the model words the answer from that result.

The model never writes a query, never does the arithmetic (amounts and who owes whom arrive worked out) and
sees nothing but the tool's result. Only a System Manager can ask, as for the dashboard. Nothing is stored
here: the conversation lives in the browser and comes back with each question, and AI Interface logs the
calls.
"""

import json
import re

import frappe
from frappe import _
from frappe.utils import add_days, getdate, today

from accounts_intelligence import reconcile
from accounts_intelligence.sync import ENTITY

CALLING_APP = "accounts_intelligence"
MAX_QUESTION = 1000
MAX_TURNS = 4
MAX_LINES = 50
# Lines handed back to the page with the answer, so the figures can be checked.
SHOWN_LINES = 10


@frappe.whitelist(methods=["GET"])
def status() -> dict:
	"""Whether the assistant can answer, and why not if it cannot."""
	frappe.only_for("System Manager")
	reason = _unavailable()
	return {"available": not reason, "reason": reason}


@frappe.whitelist(methods=["POST"])
def ask(question: str, history: str | list | None = None) -> dict:
	"""Answer a question about the group's reconciliation. `history` is the recent turns the page keeps, as
	[{"question", "answer"}]."""
	frappe.only_for("System Manager")
	reason = _unavailable()
	if reason:
		return {"ok": False, "answer": reason}
	question = (question or "").strip()
	if not question:
		frappe.throw(_("Ask a question first."))
	if len(question) > MAX_QUESTION:
		frappe.throw(_("That question is too long. Try a shorter one."))
	turns = _history(history)

	try:
		_check_rate()
		companies = _companies()
		plan = _parse_json(_call(_plan_prompt(question, companies, turns), "plan"))
		if plan.get("tool") == "none":
			return {
				"ok": True,
				"answer": str(plan.get("reply") or _("Ask me about the group's reconciliation.")),
			}
		tool, args = _validate(plan, companies)
		facts, lines = TOOLS[tool](companies, **args)
		answer = _call(_answer_prompt(question, tool, facts, turns), "answer")
	except (Refused, *_limit_errors()) as e:
		# An expected refusal is an answer, not a failure: say it plainly.
		return {"ok": False, "answer": str(e)}
	except Exception:
		# A failed AI call carries the provider's error and traceback; those go to the logs, not the page.
		frappe.log_error(title="Accounts Intelligence: assistant failed")
		return {
			"ok": False,
			"answer": _("That did not work. See the AI Call Log or the Error Log, or try again."),
		}
	return {"ok": True, "answer": answer.strip(), "tool": tool, "args": args, "lines": lines[:SHOWN_LINES]}


class Refused(frappe.ValidationError):
	pass


def _check_rate():
	from ai_interface.services import rate_limit

	rate_limit.check(frappe.session.user, scope=CALLING_APP)


def _limit_errors() -> tuple:
	"""AI Interface's budget and rate-limit refusals, which are messages for the user."""
	try:
		from ai_interface.services import budget, rate_limit
	except ImportError:
		return ()
	return (budget.BudgetExceeded, rate_limit.RateLimited)


def _unavailable() -> str | None:
	if "ai_interface" not in frappe.get_installed_apps():
		return _("Install the AI Interface app to use the assistant.")
	if not frappe.db.get_single_value("AI Settings", "default_provider"):
		return _("Set a default provider in AI Settings to use the assistant.")
	return None


def _call(prompt: str, action: str) -> str:
	from ai_interface.services.ai_client import call_ai

	return call_ai(
		prompt=prompt,
		calling_app=CALLING_APP,
		function_type="Query",
		sync=True,
		temperature=0,
		action=action,
	)


def _history(history) -> list[dict]:
	"""The last few turns, checked: they are only context, so anything malformed is dropped."""
	if isinstance(history, str):
		try:
			history = json.loads(history or "[]")
		except ValueError:
			return []
	if not isinstance(history, list):
		return []
	turns = []
	for turn in history[-MAX_TURNS:]:
		if isinstance(turn, dict):
			turns.append({k: str(turn.get(k) or "")[:2000] for k in ("question", "answer")})
	return turns


# ── companies ───────────────────────────────────────────────────────────────


def _companies() -> dict:
	"""Group companies by PAN, with the short name people use for them."""
	return {
		e.name: frappe._dict(pan=e.name, name=e.company_name, short=_acronym(e.company_name))
		for e in frappe.get_all(ENTITY, fields=["name", "company_name"], order_by="company_name")
	}


def _acronym(name: str) -> str:
	""" "Garg Global Innovative Limited" → "GGIL", as on the dashboard."""
	words = (name or "").split()
	return "".join(re.sub(r"[^A-Za-z]", "", w)[: None if "." in w else 1] for w in words).upper()


def _company(value, companies) -> str | None:
	"""The PAN for a company the model named by PAN, short name or (part of) its name."""
	if not value:
		return None
	text = str(value).strip().upper()
	for pan, c in companies.items():
		if text in (pan, c.short):
			return pan
	matches = [pan for pan, c in companies.items() if text in c.name.upper()]
	if len(matches) == 1:
		return matches[0]
	raise Refused(
		_("I could not tell which company {0} is. Use its short name, such as {1}.").format(
			value, ", ".join(c.short for c in companies.values())
		)
	)


# ── tools ───────────────────────────────────────────────────────────────────


def group_summary(companies) -> tuple[dict, list]:
	pairs = []
	for a, b in reconcile.pairs():
		data = reconcile.reconcile(a, b)
		pairs.append(_pair_facts(data, companies))
	return {"pairs": pairs}, []


def pair_summary(companies, a, b) -> tuple[dict, list]:
	data = reconcile.reconcile(*sorted((a, b)))
	facts = _pair_facts(data, companies)
	parts = {}
	for line in data["lines"]:
		if line["status"] != "matched":
			key = f"{line['kind']}: {_status(line['status'], data, companies)}"
			part = parts.setdefault(key, {"what": key, "lines": 0, "amount": 0.0})
			part["lines"] += 1
			part["amount"] += line["difference"]
	facts["difference_made_of"] = [
		{**p, "amount": _inr(p["amount"])} for p in sorted(parts.values(), key=lambda p: -abs(p["amount"]))
	]
	facts["open_items_by_age"] = [
		{"age": b["bucket"], "lines": b["count"], "amount": _inr(b["amount"])}
		for b in data["trend"]["ageing"]
	]
	return facts, []


def open_items(
	companies,
	a=None,
	b=None,
	kind=None,
	status=None,
	recorded_by=None,
	from_date=None,
	to_date=None,
	older_than_days=None,
	min_amount=None,
	sort="amount",
	limit=20,
) -> tuple[dict, list]:
	found = []
	for x, y in reconcile.pairs():
		if (a and a not in (x, y)) or (b and b not in (x, y)):
			continue
		data = reconcile.reconcile(x, y, from_date, to_date)
		for line in data["lines"]:
			if line["status"] == "matched" or (kind and line["kind"] != kind):
				continue
			if status and reconcile.OUTCOME[line["status"]] != status:
				continue
			if recorded_by and not _recorded_only_by(line, data, recorded_by):
				continue
			if older_than_days and getdate(line["date"]) > getdate(add_days(today(), -older_than_days)):
				continue
			if min_amount and abs(line["difference"]) < min_amount:
				continue
			found.append(_line(line, data, companies))

	if sort == "amount":
		found.sort(key=lambda line: -abs(line["difference"]))
	else:
		found.sort(key=lambda line: line["date"], reverse=sort == "newest")
	facts = {
		"lines_found": len(found),
		"total_amount": _inr(sum(abs(line["difference"]) for line in found)),
		"lines": [_line_facts(line) for line in found[:limit]],
	}
	if len(found) > limit:
		facts["note"] = f"Only the first {limit} of {len(found)} lines are listed."
	return facts, found


def find_document(companies, text) -> tuple[dict, list]:
	needle = text.strip().lower()
	found = []
	for x, y in reconcile.pairs():
		data = reconcile.reconcile(x, y)
		for line in data["lines"]:
			docs = [d for d in (line["a"], line["b"]) if d]
			values = [str(d.get(k) or "").lower() for d in docs for k in ("name", "bill_no", "reference_no")]
			if any(needle in v for v in values):
				found.append(_line(line, data, companies))
	return {"lines_found": len(found), "lines": [_line_facts(line) for line in found[:MAX_LINES]]}, found


TOOLS = {
	"group_summary": group_summary,
	"pair_summary": pair_summary,
	"open_items": open_items,
	"find_document": find_document,
}


def _pair_facts(data, companies) -> dict:
	a, b, balance, summary = data["a"], data["b"], data["balance"], data["summary"]
	return {
		"pair": f"{_short(a, companies)} ↔ {_short(b, companies)}",
		f"balance_in_{_short(a, companies)}_books": _owes(balance["a"], a, b, companies),
		f"balance_in_{_short(b, companies)}_books": _owes(balance["b"], b, a, companies),
		"balance_difference": _inr(abs(balance["difference"])),
		"not_explained_by_any_line": None
		if summary["unexplained"] is None
		else _inr(abs(summary["unexplained"])),
		"invoices": _counts(summary["invoice"]),
		"payments_and_journals": _counts(summary["money"]),
		"balances_as_of": balance["as_of"],
	}


def _counts(counts) -> dict:
	return {
		"matched": counts["matched"],
		"amount_differs": counts["amount_differs"],
		"date_differs": counts["date_differs"],
		"recorded_on_one_side_only": counts["only_a"] + counts["only_b"],
	}


def _line(line, data, companies) -> dict:
	"""A line as the page shows it: the reconcile line with its pair and a readable status."""
	return {
		**line,
		"pair": {"a": data["a"], "b": data["b"]},
		"status_label": _status(line["status"], data, companies),
	}


def _line_facts(line) -> dict:
	def doc(d):
		if not d:
			return "not recorded"
		ref = f", bill {d['bill_no']}" if d.get("bill_no") else ""
		return f"{d['doctype']} {d['name']} dated {d['date']} for {_inr(abs(d['amount']))}{ref}"

	a, b = line["pair"]["a"], line["pair"]["b"]
	return {
		"date": line["date"],
		"status": line["status_label"],
		f"{_acronym(a['name'])}": doc(line["a"]),
		f"{_acronym(b['name'])}": doc(line["b"]),
		"difference": _inr(abs(line["difference"])),
	}


def _recorded_only_by(line, data, pan) -> bool:
	return (line["status"] == "only_a" and data["a"]["pan"] == pan) or (
		line["status"] == "only_b" and data["b"]["pan"] == pan
	)


def _status(status, data, companies) -> str:
	return {
		"matched": "matched",
		"amount_differs": "amount differs",
		"date_differs": "date differs",
		"only_a": f"only in {_short(data['a'], companies)}'s books",
		"only_b": f"only in {_short(data['b'], companies)}'s books",
	}[status]


def _short(company, companies) -> str:
	return companies.get(company["pan"], frappe._dict(short=company["pan"])).short


def _owes(balance, holder, other, companies) -> str:
	if balance is None:
		return "no balance received"
	if abs(balance) < 0.005:
		return "settled"
	debtor, creditor = (other, holder) if balance > 0 else (holder, other)
	return f"{_short(debtor, companies)} owes {_short(creditor, companies)} {_inr(abs(balance))}"


def _inr(value) -> str:
	"""₹7,75,61,783.00: Indian grouping, the way the finance team reads amounts."""
	sign = "-" if value < 0 else ""
	whole, fraction = f"{abs(value):.2f}".split(".")
	if len(whole) > 3:
		head, tail = whole[:-3], whole[-3:]
		groups = [head[max(i - 2, 0) : i] for i in range(len(head), 0, -2)]
		whole = ",".join(reversed(groups)) + "," + tail
	return f"{sign}₹{whole}.{fraction}"


# ── planning ────────────────────────────────────────────────────────────────

KINDS = {
	"invoice": "Invoice",
	"invoices": "Invoice",
	"money": "Money",
	"payment": "Money",
	"payments": "Money",
}
STATUSES = ("missing", "differs")
SORTS = ("amount", "oldest", "newest")


def _validate(plan, companies) -> tuple[str, dict]:
	"""Check the model's choice against the tool's real arguments. Anything unknown is refused, not
	guessed."""
	tool = plan.get("tool")
	if tool not in TOOLS:
		raise Refused(
			_("I could not work out how to answer that. Try asking about a company pair or open items.")
		)
	raw = plan.get("args") if isinstance(plan.get("args"), dict) else {}
	args = {}
	if tool in ("pair_summary", "open_items"):
		for key in ("a", "b"):
			if raw.get(key):
				args[key] = _company(raw[key], companies)
		if tool == "pair_summary" and not (args.get("a") and args.get("b") and args["a"] != args["b"]):
			raise Refused(_("Name the two companies of the pair."))
	if tool == "open_items":
		if raw.get("kind"):
			args["kind"] = KINDS.get(str(raw["kind"]).lower())
		if raw.get("status") in STATUSES:
			args["status"] = raw["status"]
		if raw.get("recorded_by"):
			args["recorded_by"] = _company(raw["recorded_by"], companies)
		for key in ("from_date", "to_date"):
			if raw.get(key):
				try:
					args[key] = str(getdate(raw[key]))
				except Exception:
					raise Refused(_("{0} is not a date I can use.").format(raw[key]))
		for key, cast in (("older_than_days", int), ("min_amount", float), ("limit", int)):
			if raw.get(key) not in (None, ""):
				try:
					args[key] = max(cast(raw[key]), 0)
				except (TypeError, ValueError):
					pass
		args["limit"] = min(args.get("limit") or 20, MAX_LINES)
		args["sort"] = raw.get("sort") if raw.get("sort") in SORTS else "amount"
	if tool == "find_document":
		text = str(raw.get("text") or "").strip()
		if len(text) < 3:
			raise Refused(_("Give at least three characters of the document or bill number."))
		args["text"] = text[:60]
	return tool, args


def _parse_json(raw: str) -> dict:
	text = (raw or "").strip()
	start, end = text.find("{"), text.rfind("}")
	if start >= 0 and end > start:
		try:
			value = json.loads(text[start : end + 1])
			if isinstance(value, dict):
				return value
		except ValueError:
			pass
	raise Refused(_("I could not understand that. Try rephrasing."))


def _plan_prompt(question, companies, turns) -> str:
	names = "\n".join(f"- {c.short}: {c.name} (PAN {c.pan})" for c in companies.values())
	pairs = "\n".join(
		f"- {companies[a].short} ↔ {companies[b].short}"
		for a, b in reconcile.pairs()
		if a in companies and b in companies
	)
	return f"""You help the finance head of a group of Indian companies check what the group companies recorded
about each other. Each company runs its own books. Choose ONE tool that answers the question.

Reply with JSON only, no prose: {{"tool": "<name>", "args": {{...}}}}
For a greeting or a question these tools cannot answer: {{"tool": "none", "reply": "<one short sentence>"}}

TODAY: {today()}

COMPANIES (refer to them by short name)
{names}

COMPANY PAIRS WITH DATA
{pairs}

TOOLS
- group_summary {{}}: every pair's balances, balance difference, matched and open counts.
- pair_summary {{"a": company, "b": company}}: one pair's balances, who owes whom, what makes up the
  difference, and open items by age.
- open_items {{"a"?: company, "b"?: company, "kind"?: "invoice"|"money", "status"?: "missing"|"differs",
  "recorded_by"?: company, "from_date"?: "YYYY-MM-DD", "to_date"?: "YYYY-MM-DD", "older_than_days"?: number,
  "min_amount"?: number, "sort"?: "amount"|"oldest"|"newest", "limit"?: number}}: documents that do not
  match. "missing" means recorded by one company only; "recorded_by" is the company that has it when the
  other does not. "money" means payments and journal entries.
- find_document {{"text": "..."}}: a document, bill or bank reference number, matched or not.
{_history_block(turns)}
QUESTION: {question}

JSON:"""


def _answer_prompt(question, tool, facts, turns) -> str:
	return f"""Answer the finance head's question using only the data below.

RULES
- Be direct and brief: the answer first, then the few facts that support it.
- Quote amounts exactly as written in the data, and name document numbers when they matter.
- Short paragraphs; "- " bullets for lists. No headings, no tables, no JSON or field names.
- If the data does not answer the question, say so. Never estimate or invent.
{_history_block(turns)}
QUESTION: {question}

DATA ({tool.replace("_", " ")}):
{json.dumps(facts, default=str, indent=1, ensure_ascii=False)[:9000]}

ANSWER:"""


def _history_block(turns) -> str:
	if not turns:
		return ""
	lines = []
	for turn in turns:
		lines += [f"User: {turn['question']}", f"Assistant: {turn['answer']}"]
	return "\nEARLIER IN THIS CONVERSATION (for follow-up questions)\n" + "\n".join(lines) + "\n"

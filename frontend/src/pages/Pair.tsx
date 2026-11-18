import { useEffect, useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getPair, reconcileByHand, undoMatch, useLoad, type Company, type Doc, type Line, type PairResult } from "../api";
import { AgeingBars } from "../components/Charts";
import Icon from "../components/Icon";
import {
  STATUS_STYLE, TONE_TEXT, acronym, csvRows, date, docType, docUrl, gapTone, lineDocs, money, moneyShort, owes, short,
  statusLabel,
} from "../format";
import { Message, PeriodFilter, downloadCsv, useShell } from "../Layout";

type Show = "open" | "matched" | "all";
type Kind = "all" | "Invoice" | "Money";

/** Differences up to this many rupees count as matched, as on the server. */
const TOLERANCE = 1;

/** An open document picked for reconciling by hand. */
interface Picked {
  entity: string;
  doc: Doc;
  kind: Line["kind"];
}

const pickKey = (entity: string, doc: Doc) => `${entity}|${doc.doctype}|${doc.name}`;

/** What a document adds to the balance difference: the seller's invoices less the buyer's; money is already
 * signed. A set of documents can be reconciled when these add up to zero. */
const contribution = (kind: Line["kind"], doc: Doc) =>
  kind === "Invoice" && doc.doctype === "Purchase Invoice" ? -doc.amount : doc.amount;

/** What reconciling by hand needs from the table. */
interface Picking {
  picked: (entity: string, doc: Doc) => boolean;
  allowed: (entity: string, doc: Doc, kind: Line["kind"]) => boolean;
  fits: (entity: string, doc: Doc, kind: Line["kind"]) => boolean;
  toggle: (entity: string, doc: Doc, kind: Line["kind"]) => void;
  findMatch: (line: Line) => void;
  undo: (line: Line) => void;
}

export default function Pair() {
  const { a = "", b = "" } = useParams();
  const { period, setPeriod, query } = useShell();
  // Bumped after reconciling or undoing, to load the pair again.
  const [version, setVersion] = useState(0);
  const { data, error, loading } = useLoad(() => getPair(a, b, period), `${a}|${b}|${query}|${version}`);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div className="min-w-0">
          <div className="flex items-center gap-1 text-sm text-ink-3">
            <Link to={`/${query}`} className="hover:text-ink">Dashboard</Link>
            <Icon name="chevronRight" className="h-3.5 w-3.5" />
            <span>{data ? `${acronym(data.a.name)} ↔ ${acronym(data.b.name)}` : "Company pair"}</span>
          </div>
          <h1 className="truncate text-2xl font-semibold tracking-tight">
            {data ? `${short(data.a.name)} ↔ ${short(data.b.name)}` : "Reconciliation"}
          </h1>
        </div>
        <PeriodFilter period={period} onChange={setPeriod} />
      </div>

      {error && <Message tone="error">{error}</Message>}
      {!error && loading && !data && <Message>Loading…</Message>}
      {data && (
        <div className={`space-y-5 transition-opacity ${loading ? "opacity-60" : ""}`}>
          <Balances data={data} />
          <div className="grid gap-5 lg:grid-cols-2">
            <GapParts data={data} />
            <section className="card p-4">
              <h2 className="font-semibold tracking-tight">Ageing of open items</h2>
              <p className="mb-4 text-xs text-ink-3">Days since the document date, with the amount at stake</p>
              <AgeingBars ageing={data.trend.ageing} />
            </section>
          </div>
          <BrokenMatches data={data} onChanged={() => setVersion((v) => v + 1)} />
          <Lines data={data} onChanged={() => setVersion((v) => v + 1)} />
        </div>
      )}
    </div>
  );
}

function Balances({ data }: { data: PairResult }) {
  const { a, b, balance, summary } = data;
  const tone = gapTone(balance.difference, summary.open);
  return (
    <div className="grid gap-4 md:grid-cols-[1fr_auto_1fr_1.2fr] md:items-stretch">
      <BookCard company={a} balance={balance.a} other={b} />
      <div className="hidden items-center justify-center text-2xl font-light text-ink-3 md:flex">{Math.abs(balance.difference) > 1 ? "≠" : "="}</div>
      <BookCard company={b} balance={balance.b} other={a} />
      <div className={`card flex flex-col justify-between p-4 ${tone === "good" ? "" : "border-rose-200 dark:border-rose-500/30"}`}>
        <span className="caps">Difference</span>
        <div className={`mt-1 text-2xl font-semibold tabular-nums ${TONE_TEXT[tone]}`}>{money(balance.difference)}</div>
        <div className="mt-2 text-xs text-ink-3">
          {summary.unexplained == null
            ? "Pick all time to see how much the lines explain."
            : Math.abs(summary.unexplained) <= 1
              ? "Fully explained by the open lines below."
              : `${money(summary.unexplained)} not explained by any line: documents before the sync start, or a party without a PAN.`}
          {balance.as_of && <span className="block">Balances as of {date(balance.as_of)}</span>}
        </div>
      </div>
    </div>
  );
}

function BookCard({ company, balance, other }: { company: Company; balance: number | null; other: Company }) {
  return (
    <div className="card p-4">
      <span className="caps">In {acronym(company.name)}'s books</span>
      <div className="mt-1 text-2xl font-semibold tabular-nums" title={balance == null ? undefined : money(balance)}>
        {balance == null ? "—" : moneyShort(Math.abs(balance))}
      </div>
      <div className="mt-2 text-xs text-ink-3">{owes(balance, company, other)}</div>
    </div>
  );
}

/** What the open lines add up to, by kind and side: the parts of the balance difference. */
function GapParts({ data }: { data: PairResult }) {
  const { a, b, lines, summary } = data;
  const parts = new Map<string, { label: string; amount: number; count: number }>();
  for (const line of lines) {
    if (line.status === "matched") continue;
    const key = `${line.kind}|${line.status}`;
    const label = `${line.kind === "Invoice" ? "Invoices" : "Payments & journals"}: ${statusLabel(line.status, a, b).toLowerCase()}`;
    const part = parts.get(key) ?? { label, amount: 0, count: 0 };
    part.amount += line.difference;
    part.count += 1;
    parts.set(key, part);
  }
  const rows = [...parts.values()].sort((x, y) => Math.abs(y.amount) - Math.abs(x.amount));
  if (summary.unexplained != null && Math.abs(summary.unexplained) > 1) {
    rows.push({ label: "Not explained by any line", amount: summary.unexplained, count: 0 });
  }
  const max = Math.max(...rows.map((r) => Math.abs(r.amount)), 1);

  return (
    <section className="card p-4">
      <h2 className="font-semibold tracking-tight">What makes up the difference</h2>
      <p className="mb-4 text-xs text-ink-3">Open lines grouped by type and side</p>
      {rows.length === 0 ? (
        <div className="flex h-32 items-center justify-center text-sm text-emerald-700 dark:text-emerald-400">Every line matches</div>
      ) : (
        <div className="space-y-3">
          {rows.map((row) => (
            <div key={row.label}>
              <div className="mb-1 flex items-baseline justify-between gap-3 text-sm">
                <span className="truncate first-letter:uppercase">
                  {row.label}
                  {row.count > 0 && <span className="ml-1.5 text-xs text-ink-3">{row.count}</span>}
                </span>
                <span className="font-semibold tabular-nums" title={money(row.amount)}>{moneyShort(row.amount)}</span>
              </div>
              <div className="h-1.5 overflow-hidden rounded-full bg-sunken">
                <div className={`h-1.5 rounded-full ${row.count ? "bg-rose-500" : "bg-slate-400"}`}
                  style={{ width: `${Math.max((Math.abs(row.amount) / max) * 100, 1.5)}%` }} />
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

function Lines({ data, onChanged }: { data: PairResult; onChanged: () => void }) {
  const { a, b, lines } = data;
  const [show, setShow] = useState<Show>("open");
  const [kind, setKind] = useState<Kind>("all");
  const [search, setSearch] = useState("");
  const [chosen, setChosen] = useState<Map<string, Picked>>(new Map());
  const [fitOnly, setFitOnly] = useState(false);
  const [problem, setProblem] = useState<string | null>(null);

  // Fresh lines (after a reload or another period) start a new selection.
  useEffect(() => {
    setChosen(new Map());
    setFitOnly(false);
  }, [lines]);

  const list = [...chosen.values()];
  const net = list.reduce((sum, p) => sum + contribution(p.kind, p.doc), 0);

  const allowed = (entity: string, doc: Doc, lineKind: Line["kind"]) => {
    if (!list.length) return true;
    const first = list[0];
    if (first.kind !== lineKind) return false;
    if (lineKind === "Money") return true;
    // Invoices go one way: the seller's sales invoices against the buyer's purchase invoices.
    const other = (pan: string) => (pan === a.pan ? b.pan : a.pan);
    const seller = first.doc.doctype === "Sales Invoice" ? first.entity : other(first.entity);
    return doc.doctype === "Sales Invoice" ? entity === seller : entity !== seller;
  };

  const picking: Picking = {
    picked: (entity, doc) => chosen.has(pickKey(entity, doc)),
    allowed,
    fits: (entity, doc, lineKind) =>
      list.length > 0 && Math.abs(net) > TOLERANCE && !chosen.has(pickKey(entity, doc)) && allowed(entity, doc, lineKind)
      && Math.abs(net + contribution(lineKind, doc)) <= TOLERANCE,
    toggle: (entity, doc, lineKind) => {
      setProblem(null);
      setChosen((current) => {
        const next = new Map(current);
        const key = pickKey(entity, doc);
        if (next.has(key)) next.delete(key);
        else next.set(key, { entity, doc, kind: lineKind });
        if (!next.size) setFitOnly(false);
        return next;
      });
    },
    findMatch: (line) => {
      setProblem(null);
      const next = new Map<string, Picked>();
      for (const [entity, doc] of [[a.pan, line.a], [b.pan, line.b]] as const) {
        if (doc) next.set(pickKey(entity, doc), { entity, doc, kind: line.kind });
      }
      setChosen(next);
      setFitOnly(true);
    },
    undo: async (line) => {
      if (!line.match || !window.confirm("Undo this match? Its documents go back to the automatic matching, or open.")) return;
      try {
        await undoMatch(line.match.name);
        onChanged();
      } catch (e) {
        setProblem((e as Error).message);
      }
    },
  };

  const shown = useMemo(() => {
    const text = search.trim().toLowerCase();
    return lines.filter((line) => {
      if (fitOnly && list.length) {
        // The chosen documents and those that would close the gap, whatever the other filters say.
        return line.status !== "matched" && ([[a.pan, line.a], [b.pan, line.b]] as const).some(
          ([entity, doc]) => doc && (picking.picked(entity, doc) || picking.fits(entity, doc, line.kind)),
        );
      }
      if (show === "open" && line.status === "matched") return false;
      if (show === "matched" && line.status !== "matched") return false;
      if (kind !== "all" && line.kind !== kind) return false;
      if (!text) return true;
      return [...lineDocs(line, "a"), ...lineDocs(line, "b")].some((doc) =>
        [doc.name, doc.bill_no, doc.reference_no, doc.party].some((v) => v?.toLowerCase().includes(text)),
      );
    });
    // `picking` reads `chosen` and `net`, which `chosen` stands for.
  }, [lines, show, kind, search, fitOnly, chosen]);

  const open = lines.filter((l) => l.status !== "matched").length;
  const manual = data.summary.manual;
  const tabs: [Show, string][] = [
    ["open", `Open ${open}`],
    ["matched", `Matched ${lines.length - open}${manual ? ` · ${manual} by hand` : ""}`],
    ["all", `All ${lines.length}`],
  ];

  return (
    <section className="card overflow-hidden">
      <div className="flex flex-wrap items-center gap-2 border-b border-line p-3">
        <div className="flex rounded-lg bg-sunken p-0.5">
          {tabs.map(([value, label]) => (
            <button key={value} onClick={() => setShow(value)}
              className={`rounded-md px-3 py-1 text-sm tabular-nums transition-colors ${show === value ? "bg-surface font-medium shadow-card" : "text-ink-2 hover:text-ink"}`}>
              {label}
            </button>
          ))}
        </div>
        <select className="field" value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="all">Invoices and money</option>
          <option value="Invoice">Invoices</option>
          <option value="Money">Payments and journals</option>
        </select>
        <div className="relative min-w-0 flex-1 sm:max-w-xs">
          <Icon name="search" className="pointer-events-none absolute left-2.5 top-2 h-4 w-4 text-ink-3" />
          <input className="field w-full pl-8" placeholder="Document, bill no, reference, party" value={search}
            onChange={(e) => setSearch(e.target.value)} />
        </div>
        <button className="btn ml-auto" disabled={!shown.length}
          onClick={() => downloadCsv(`reconciliation-${a.pan}-${b.pan}.csv`, csvRows(shown, a, b))}>
          <Icon name="download" /> CSV
        </button>
      </div>

      {shown.length === 0 ? (
        <div className="px-4 py-10 text-center text-sm text-ink-3">
          {lines.length ? "Nothing matches these filters." : "No documents between these companies in this period."}
        </div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[920px] text-sm">
            <thead className="bg-sunken/60 text-left">
              <tr className="caps">
                <th className="px-4 py-2.5 font-semibold">Date</th>
                <th className="px-4 py-2.5 font-semibold">Status</th>
                <th className="px-4 py-2.5 font-semibold">{acronym(a.name)}'s document</th>
                <th className="px-4 py-2.5 font-semibold">{acronym(b.name)}'s document</th>
                <th className="px-4 py-2.5 text-right font-semibold">Difference</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-line">
              {shown.map((line, i) => (
                <LineRow key={`${line.key}|${i}`} line={line} a={a} b={b} picking={picking} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      {problem && !list.length && <div className="border-t border-line px-4 py-2.5 text-sm text-rose-700 dark:text-rose-400">{problem}</div>}
      {list.length > 0 && (
        <MatchBar a={a} b={b} list={list} net={net} fitOnly={fitOnly} problem={problem}
          onFitOnly={setFitOnly} onProblem={setProblem}
          onClear={() => { setChosen(new Map()); setFitOnly(false); setProblem(null); }}
          onDone={onChanged} />
      )}
      <p className="border-t border-line px-4 py-2.5 text-xs text-ink-3">
        Amounts in company currency. Invoices pair by the buyer's bill number, then by date and amount; payments and
        journals pair when they cancel each other within 3 days. Differences under ₹1 count as matched. Entries the
        matching leaves open, such as the same amount booked on different days, can be reconciled by hand: tick them on
        both sides, or use Find match on a line.
      </p>
    </section>
  );
}

/** The chosen documents, what they still differ by, and the button that reconciles them. */
function MatchBar({ a, b, list, net, fitOnly, problem, onFitOnly, onProblem, onClear, onDone }: {
  a: Company; b: Company; list: Picked[]; net: number; fitOnly: boolean; problem: string | null;
  onFitOnly: (on: boolean) => void; onProblem: (message: string | null) => void; onClear: () => void; onDone: () => void;
}) {
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const side = (company: Company) => {
    const docs = list.filter((p) => p.entity === company.pan);
    const total = Math.abs(docs.reduce((sum, p) => sum + p.doc.amount, 0));
    return { docs, total };
  };
  const sideA = side(a);
  const sideB = side(b);
  const agree = Math.abs(net) <= TOLERANCE;
  const ready = sideA.docs.length > 0 && sideB.docs.length > 0 && agree && note.trim().length > 0;

  // Entries far apart, or in different financial years, are a timing difference worth a second look.
  const dates = list.map((p) => p.doc.date).sort();
  const days = dates.length ? Math.round((Date.parse(dates[dates.length - 1]) - Date.parse(dates[0])) / 86400000) : 0;
  const years = [...new Set(dates.map(financialYear))];
  const caution = years.length > 1
    ? `These entries fall in different financial years (${years.join(", ")}): check the timing difference in both years' accounts.`
    : days > 90 ? `These entries are ${days} days apart.` : null;

  const save = async () => {
    setBusy(true);
    onProblem(null);
    try {
      await reconcileByHand(a.pan, b.pan, list.map((p) => ({ entity: p.entity, doctype: p.doc.doctype, name: p.doc.name })), note);
      setNote("");
      onDone();
    } catch (e) {
      onProblem((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const summary = (company: Company, part: { docs: Picked[]; total: number }) => (
    <div className="min-w-0">
      <div className="caps">{acronym(company.name)}</div>
      <div className="tabular-nums">
        {part.docs.length ? <>{part.docs.length} {part.docs.length === 1 ? "entry" : "entries"} · <b>{money(part.total)}</b></> : <span className="text-ink-3">Choose an entry</span>}
      </div>
    </div>
  );

  return (
    <div className="sticky bottom-0 z-10 border-t border-accent/40 bg-accent-soft/80 px-4 py-3 backdrop-blur">
      <div className="flex flex-wrap items-center gap-x-6 gap-y-3 text-sm">
        {summary(a, sideA)}
        {summary(b, sideB)}
        <div>
          <div className="caps">Difference</div>
          <div className={`font-semibold tabular-nums ${agree ? "text-emerald-700 dark:text-emerald-400" : "text-rose-700 dark:text-rose-400"}`}>
            {money(Math.abs(net))}
          </div>
        </div>
        <label className="flex items-center gap-1.5 text-xs text-ink-2">
          <input type="checkbox" checked={fitOnly} onChange={(e) => onFitOnly(e.target.checked)} />
          Only entries that fit
        </label>
        <input className="field min-w-0 flex-1 sm:max-w-xs" maxLength={1000} placeholder="Note: why these belong together (required)"
          aria-label="Why these entries belong together" value={note} onChange={(e) => setNote(e.target.value)} />
        <div className="ml-auto flex gap-2">
          <button className="btn h-8 text-xs" onClick={onClear} disabled={busy}>Clear</button>
          <button className="btn-primary disabled:cursor-not-allowed disabled:opacity-50" onClick={save} disabled={!ready || busy}>
            <Icon name="check" /> {busy ? "Reconciling…" : "Reconcile"}
          </button>
        </div>
      </div>
      <p className="mt-1.5 text-xs text-ink-3">
        {problem ? <span className="text-rose-700 dark:text-rose-400">{problem}</span>
          : !sideA.docs.length || !sideB.docs.length ? "Choose entries from both companies."
          : !agree ? "The totals must agree within ₹1. Entries marked Fits would close the gap."
          : !note.trim() ? "The totals agree. Write why these entries belong together, then Reconcile."
          : "Reconcile marks them matched; you can undo it from the Matched tab."}
      </p>
      {caution && <p className="mt-1 text-xs text-amber-700 dark:text-amber-400">{caution}</p>}
    </div>
  );
}

/** 2026-03-31 → FY 2025-26; 2026-04-01 → FY 2026-27 (April to March). */
function financialYear(value: string) {
  const [y, m] = value.split("-").map(Number);
  const start = m >= 4 ? y : y - 1;
  return `FY ${start}-${String((start + 1) % 100).padStart(2, "0")}`;
}

/** Matches made by hand that no longer hold, with why, so someone can undo them. */
function BrokenMatches({ data, onChanged }: { data: PairResult; onChanged: () => void }) {
  const [problem, setProblem] = useState<string | null>(null);
  if (!data.broken_matches?.length) return null;
  const company = (pan: string) => acronym(pan === data.a.pan ? data.a.name : data.b.name);
  const undo = async (name: string) => {
    if (!window.confirm("Undo this match? Its remaining documents are matched automatically again, or open.")) return;
    try {
      await undoMatch(name);
      onChanged();
    } catch (e) {
      setProblem((e as Error).message);
    }
  };
  return (
    <section className="card border-amber-200 p-4 dark:border-amber-500/30">
      <h2 className="flex items-center gap-2 font-semibold tracking-tight">
        <Icon name="alert" className="h-4 w-4 text-amber-600" />
        {data.broken_matches.length === 1 ? "A match made by hand no longer holds" : `${data.broken_matches.length} matches made by hand no longer hold`}
      </h2>
      <p className="mb-3 text-xs text-ink-3">Their documents are counted as if the match were not there. Check why, then undo it.</p>
      <div className="divide-y divide-line">
        {data.broken_matches.map((m) => (
          <div key={m.name} className="flex flex-wrap items-start justify-between gap-3 py-2 text-sm">
            <div className="min-w-0">
              <div className="font-medium">{m.reason}</div>
              <div className="text-xs text-ink-3">
                {m.documents.map((d) => `${company(d.entity)} ${d.name}`).join(" + ")}
                {" · "}by {m.by_name ?? m.by}{m.on ? `, ${date(m.on.slice(0, 10))}` : ""}{m.note ? ` · ${m.note}` : ""}
              </div>
            </div>
            <button className="btn h-7 text-xs" onClick={() => undo(m.name)}>Undo</button>
          </div>
        ))}
      </div>
      {problem && <p className="mt-2 text-xs text-rose-700 dark:text-rose-400">{problem}</p>}
    </section>
  );
}

function LineRow({ line, a, b, picking }: { line: Line; a: Company; b: Company; picking: Picking }) {
  const matched = line.status === "matched";
  return (
    <tr className="align-top hover:bg-sunken/40">
      <td className="whitespace-nowrap px-4 py-3 tabular-nums text-ink-2">{date(line.date)}</td>
      <td className="px-4 py-3">
        <span className={`inline-block whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[line.status]}`}>
          {statusLabel(line.status, a, b)}
        </span>
        <div className="mt-1 text-[11px] text-ink-3">{line.kind === "Invoice" ? "Invoice" : "Payment / journal"}</div>
        {line.match && (
          <div className="mt-1 max-w-[14rem] text-[11px] text-ink-2" title={line.match.note ?? undefined}>
            By hand · {line.match.by_name ?? line.match.by}{line.match.on ? `, ${date(line.match.on.slice(0, 10))}` : ""}
            {line.match.note && <div className="truncate text-ink-3">{line.match.note}</div>}
            <button className="mt-0.5 text-accent hover:underline" onClick={() => picking.undo(line)}>Undo</button>
          </div>
        )}
        {line.review && line.review.status !== "Open" && (
          <div className="mt-1 max-w-[14rem] text-[11px] text-ink-2" title={line.review.note ?? undefined}>
            {line.review.status}{line.review.reason ? `: ${line.review.reason}` : ""}
          </div>
        )}
      </td>
      <DocCell docs={lineDocs(line, "a")} company={a} line={line} picking={picking} />
      <DocCell docs={lineDocs(line, "b")} company={b} line={line} picking={picking} />
      <td className={`whitespace-nowrap px-4 py-3 text-right tabular-nums ${matched ? "text-ink-3" : "font-semibold"}`}>
        {money(line.difference)}
        {!matched && (
          <div>
            <button className="mt-1 text-xs font-normal text-accent hover:underline" onClick={() => picking.findMatch(line)}>
              Find match
            </button>
          </div>
        )}
      </td>
    </tr>
  );
}

function DocCell({ docs, company, line, picking }: { docs: Doc[]; company: Company; line: Line; picking: Picking }) {
  if (!docs.length) {
    return (
      <td className="px-4 py-3">
        <span className="inline-block rounded-md border border-dashed border-rose-300 px-2 py-1 text-xs text-rose-600 dark:border-rose-500/40 dark:text-rose-400">
          Not recorded
        </span>
      </td>
    );
  }
  return (
    <td className="space-y-2 px-4 py-3">
      {docs.map((doc) => <DocBlock key={`${doc.doctype}|${doc.name}`} doc={doc} company={company} line={line} picking={picking} />)}
    </td>
  );
}

function DocBlock({ doc, company, line, picking }: { doc: Doc; company: Company; line: Line; picking: Picking }) {
  const url = docUrl(doc, company);
  const account = doc.doctype === "Account Entry";
  const reference = doc.bill_no ? `Bill ${doc.bill_no}` : account ? undefined : doc.reference_no;
  const open = line.status !== "matched";
  const picked = open && picking.picked(company.pan, doc);
  const allowed = open && (picked || picking.allowed(company.pan, doc, line.kind));
  const fits = open && picking.fits(company.pan, doc, line.kind);
  return (
    <div className={`flex gap-2 ${picked ? "rounded-md bg-accent-soft/70 p-1 -m-1" : ""}`}>
      {open && (
        <input type="checkbox" className="mt-1 shrink-0" checked={picked} disabled={!allowed}
          aria-label={`Choose ${doc.name} to reconcile`}
          title={allowed ? "Choose to reconcile by hand" : "Reconcile invoices with invoices of the other direction, and money with money"}
          onChange={() => picking.toggle(company.pan, doc, line.kind)} />
      )}
      <div className="min-w-0">
        <div className="flex flex-wrap items-baseline gap-x-2">
          {url ? (
            <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium hover:text-accent">
              {doc.name}<Icon name="external" className="h-3 w-3 text-ink-3" />
            </a>
          ) : (
            <span className="font-medium">{doc.name}</span>
          )}
          <span className="tabular-nums">{money(doc.amount)}</span>
          {doc.date !== line.date && <span className="text-xs text-amber-700 dark:text-amber-400">{date(doc.date)}</span>}
          {fits && <span className="rounded bg-emerald-100 px-1.5 text-[10px] font-medium text-emerald-800 dark:bg-emerald-500/20 dark:text-emerald-300">Fits</span>}
        </div>
        <div className="text-xs text-ink-3">
          {docType(doc)}{doc.payment_type ? ` · ${doc.payment_type}` : ""}{doc.party ? ` · ${account ? "account " : ""}${doc.party}` : ""}
        </div>
        {reference && <div className="max-w-xs truncate text-xs text-ink-3" title={reference}>{reference}</div>}
      </div>
    </div>
  );
}

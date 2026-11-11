import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getPair, useLoad, type Company, type Doc, type Line, type PairResult } from "../api";
import { AgeingBars } from "../components/Charts";
import Icon from "../components/Icon";
import {
  STATUS_STYLE, TONE_TEXT, acronym, csvRows, date, docUrl, gapTone, money, moneyShort, owes, short, statusLabel,
} from "../format";
import { Message, PeriodFilter, downloadCsv, useShell } from "../Layout";

type Show = "open" | "matched" | "all";
type Kind = "all" | "Invoice" | "Money";

export default function Pair() {
  const { a = "", b = "" } = useParams();
  const { period, setPeriod, query } = useShell();
  const { data, error, loading } = useLoad(() => getPair(a, b, period), `${a}|${b}|${query}`);

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
          <Lines data={data} />
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

function Lines({ data }: { data: PairResult }) {
  const { a, b, lines } = data;
  const [show, setShow] = useState<Show>("open");
  const [kind, setKind] = useState<Kind>("all");
  const [search, setSearch] = useState("");

  const shown = useMemo(() => {
    const text = search.trim().toLowerCase();
    return lines.filter((line) => {
      if (show === "open" && line.status === "matched") return false;
      if (show === "matched" && line.status !== "matched") return false;
      if (kind !== "all" && line.kind !== kind) return false;
      if (!text) return true;
      return [line.a, line.b].some((doc) =>
        [doc?.name, doc?.bill_no, doc?.reference_no, doc?.party].some((v) => v?.toLowerCase().includes(text)),
      );
    });
  }, [lines, show, kind, search]);

  const open = lines.filter((l) => l.status !== "matched").length;
  const tabs: [Show, string][] = [["open", `Open ${open}`], ["matched", `Matched ${lines.length - open}`], ["all", `All ${lines.length}`]];

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
              {shown.map((line, i) => <LineRow key={`${line.a?.name}|${line.b?.name}|${i}`} line={line} a={a} b={b} />)}
            </tbody>
          </table>
        </div>
      )}
      <p className="border-t border-line px-4 py-2.5 text-xs text-ink-3">
        Amounts in company currency. Invoices pair by the buyer's bill number, then by date and amount; payments and
        journals pair when they cancel each other within 3 days. Differences under ₹1 count as matched.
      </p>
    </section>
  );
}

function LineRow({ line, a, b }: { line: Line; a: Company; b: Company }) {
  const matched = line.status === "matched";
  return (
    <tr className="align-top hover:bg-sunken/40">
      <td className="whitespace-nowrap px-4 py-3 tabular-nums text-ink-2">{date(line.date)}</td>
      <td className="px-4 py-3">
        <span className={`inline-block whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[line.status]}`}>
          {statusLabel(line.status, a, b)}
        </span>
        <div className="mt-1 text-[11px] text-ink-3">{line.kind === "Invoice" ? "Invoice" : "Payment / journal"}</div>
      </td>
      <DocCell doc={line.a} company={a} lineDate={line.date} />
      <DocCell doc={line.b} company={b} lineDate={line.date} />
      <td className={`whitespace-nowrap px-4 py-3 text-right tabular-nums ${matched ? "text-ink-3" : "font-semibold"}`}>
        {money(line.difference)}
      </td>
    </tr>
  );
}

function DocCell({ doc, company, lineDate }: { doc: Doc | null; company: Company; lineDate: string }) {
  if (!doc) {
    return (
      <td className="px-4 py-3">
        <span className="inline-block rounded-md border border-dashed border-rose-300 px-2 py-1 text-xs text-rose-600 dark:border-rose-500/40 dark:text-rose-400">
          Not recorded
        </span>
      </td>
    );
  }
  const url = docUrl(doc, company);
  const reference = doc.bill_no ? `Bill ${doc.bill_no}` : doc.reference_no;
  return (
    <td className="px-4 py-3">
      <div className="flex flex-wrap items-baseline gap-x-2">
        {url ? (
          <a href={url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1 font-medium hover:text-accent">
            {doc.name}<Icon name="external" className="h-3 w-3 text-ink-3" />
          </a>
        ) : (
          <span className="font-medium">{doc.name}</span>
        )}
        <span className="tabular-nums">{money(doc.amount)}</span>
        {doc.date !== lineDate && <span className="text-xs text-amber-700 dark:text-amber-400">{date(doc.date)}</span>}
      </div>
      <div className="text-xs text-ink-3">
        {doc.doctype}{doc.payment_type ? ` · ${doc.payment_type}` : ""}{doc.party ? ` · ${doc.party}` : ""}
      </div>
      {reference && <div className="max-w-xs truncate text-xs text-ink-3" title={reference}>{reference}</div>}
    </td>
  );
}

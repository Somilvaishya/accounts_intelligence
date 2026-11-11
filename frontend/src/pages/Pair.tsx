import { useMemo, useState } from "react";
import { Link, useParams } from "react-router-dom";
import { getPair, useLoad, type Company, type Doc, type Line, type PairResult } from "../api";
import { STATUS_STYLE, date, docUrl, money, owes, short, statusLabel, toCsv } from "../format";
import { Message, PeriodFilter, usePeriod } from "../Layout";

type Show = "open" | "all" | "matched";
type Kind = "all" | "Invoice" | "Money";

export default function Pair() {
  const { a = "", b = "" } = useParams();
  const [period, setPeriod, query] = usePeriod();
  const { data, error, loading } = useLoad(() => getPair(a, b, period), `${a}|${b}|${query}`);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <Link to={`/${query}`} className="text-sm text-slate-500 hover:text-slate-900">
            ← All companies
          </Link>
          <h1 className="text-xl font-semibold tracking-tight">
            {data ? `${short(data.a.name)} ↔ ${short(data.b.name)}` : "Reconciliation"}
          </h1>
        </div>
        <PeriodFilter period={period} onChange={setPeriod} />
      </div>

      {error && <Message tone="error">{error}</Message>}
      {!error && loading && !data && <Message>Loading…</Message>}
      {data && (
        <div className={`space-y-5 ${loading ? "opacity-60" : ""}`}>
          <Balances data={data} />
          <Lines data={data} />
        </div>
      )}
    </div>
  );
}

function Balances({ data }: { data: PairResult }) {
  const { a, b, balance, summary } = data;
  const card = "rounded-lg border border-slate-200 bg-white px-4 py-3";
  return (
    <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
      <div className={card}>
        <div className="text-xs text-slate-500">In {short(a.name)}'s books</div>
        <div className="mt-1 text-lg font-semibold tabular-nums">{money(balance.a == null ? null : Math.abs(balance.a))}</div>
        <div className="text-xs text-slate-500">{owes(balance.a, a, b)}</div>
      </div>
      <div className={card}>
        <div className="text-xs text-slate-500">In {short(b.name)}'s books</div>
        <div className="mt-1 text-lg font-semibold tabular-nums">{money(balance.b == null ? null : Math.abs(balance.b))}</div>
        <div className="text-xs text-slate-500">{owes(balance.b, b, a)}</div>
      </div>
      <div className={card}>
        <div className="text-xs text-slate-500">Balance difference</div>
        <div className={`mt-1 text-lg font-semibold tabular-nums ${Math.abs(balance.difference) > 1 ? "text-rose-700" : "text-emerald-700"}`}>
          {money(balance.difference)}
        </div>
        <div className="text-xs text-slate-500">{balance.as_of ? `Balances as of ${date(balance.as_of)}` : "No balances received yet"}</div>
      </div>
      <div className={card}>
        <div className="text-xs text-slate-500">Not explained by the lines below</div>
        <div className="mt-1 text-lg font-semibold tabular-nums">
          {summary.unexplained == null ? "—" : money(summary.unexplained)}
        </div>
        <div className="text-xs text-slate-500">
          {summary.unexplained == null
            ? "Shown for all dates only"
            : "Older than the sync start, or a party without a PAN"}
        </div>
      </div>
    </div>
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

  const download = () => {
    const blob = new Blob([toCsv(shown, a, b)], { type: "text/csv" });
    const link = document.createElement("a");
    link.href = URL.createObjectURL(blob);
    link.download = `reconciliation-${a.pan}-${b.pan}.csv`;
    link.click();
    URL.revokeObjectURL(link.href);
  };

  const open = lines.filter((l) => l.status !== "matched").length;
  const select = "rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm";

  return (
    <section className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <select className={select} value={show} onChange={(e) => setShow(e.target.value as Show)}>
          <option value="open">Open ({open})</option>
          <option value="matched">Matched ({lines.length - open})</option>
          <option value="all">All ({lines.length})</option>
        </select>
        <select className={select} value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
          <option value="all">Invoices and money</option>
          <option value="Invoice">Invoices</option>
          <option value="Money">Payments and journals</option>
        </select>
        <input className={`${select} min-w-0 flex-1 sm:max-w-xs`} placeholder="Document, bill no, reference, party"
          value={search} onChange={(e) => setSearch(e.target.value)} />
        <button className="ml-auto rounded-md border border-slate-300 bg-white px-3 py-1.5 text-sm hover:bg-slate-50 disabled:opacity-50"
          onClick={download} disabled={!shown.length}>
          Download CSV
        </button>
      </div>

      {shown.length === 0 ? (
        <Message>{lines.length ? "Nothing matches these filters." : "No documents between these companies in this period."}</Message>
      ) : (
        <div className="overflow-x-auto rounded-lg border border-slate-200 bg-white">
          <table className="w-full min-w-[900px] text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-3 py-2.5 font-medium">Date</th>
                <th className="px-3 py-2.5 font-medium">Status</th>
                <th className="px-3 py-2.5 font-medium">{short(a.name)}</th>
                <th className="px-3 py-2.5 font-medium">{short(b.name)}</th>
                <th className="px-3 py-2.5 text-right font-medium">Difference</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {shown.map((line, i) => (
                <LineRow key={`${line.a?.name}|${line.b?.name}|${i}`} line={line} a={a} b={b} />
              ))}
            </tbody>
          </table>
        </div>
      )}
      <p className="text-xs text-slate-400">
        Amounts in company currency. Invoices pair by the buyer's bill number, then by date and amount; payments and
        journals pair when they cancel each other within 3 days. Differences under ₹1 count as matched.
      </p>
    </section>
  );
}

function LineRow({ line, a, b }: { line: Line; a: Company; b: Company }) {
  return (
    <tr className="align-top">
      <td className="whitespace-nowrap px-3 py-2.5 tabular-nums text-slate-600">{date(line.date)}</td>
      <td className="px-3 py-2.5">
        <span className={`inline-block whitespace-nowrap rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[line.status]}`}>
          {statusLabel(line.status, a, b)}
        </span>
        <div className="mt-1 text-xs text-slate-400">{line.kind === "Invoice" ? "Invoice" : "Money"}</div>
      </td>
      <DocCell doc={line.a} company={a} lineDate={line.date} />
      <DocCell doc={line.b} company={b} lineDate={line.date} />
      <td className={`whitespace-nowrap px-3 py-2.5 text-right tabular-nums ${line.status === "matched" ? "text-slate-400" : "font-medium"}`}>
        {money(line.difference)}
      </td>
    </tr>
  );
}

function DocCell({ doc, company, lineDate }: { doc: Doc | null; company: Company; lineDate: string }) {
  if (!doc) return <td className="px-3 py-2.5 text-slate-300">Not recorded</td>;
  const url = docUrl(doc, company);
  const reference = doc.bill_no ? `Bill ${doc.bill_no}` : doc.reference_no;
  return (
    <td className="px-3 py-2.5">
      <div className="flex flex-wrap items-baseline gap-x-2">
        {url ? (
          <a href={url} target="_blank" rel="noreferrer" className="font-medium text-slate-900 hover:underline">{doc.name}</a>
        ) : (
          <span className="font-medium">{doc.name}</span>
        )}
        <span className="tabular-nums">{money(doc.amount)}</span>
        {doc.date !== lineDate && <span className="text-xs text-sky-700">{date(doc.date)}</span>}
      </div>
      <div className="text-xs text-slate-500">
        {doc.doctype}
        {doc.payment_type ? ` · ${doc.payment_type}` : ""}
        {doc.party ? ` · ${doc.party}` : ""}
      </div>
      {reference && <div className="max-w-xs truncate text-xs text-slate-400" title={reference}>{reference}</div>}
    </td>
  );
}

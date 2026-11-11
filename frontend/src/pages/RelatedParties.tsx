import { Fragment, type ReactNode } from "react";
import { useSearchParams } from "react-router-dom";
import { getRpt, useLoad, type RptRow } from "../api";
import Icon from "../components/Icon";
import { acronym, date, money, moneyShort } from "../format";
import { Message, PeriodFilter, downloadCsv, useShell } from "../Layout";

const COLUMNS = [
  ["sales", "Sales"],
  ["purchases", "Purchases"],
  ["received", "Received"],
  ["paid", "Paid"],
  ["journals", "Journals"],
] as const;
type Column = (typeof COLUMNS)[number][0];

const sum = (rows: RptRow[], column: Column) => rows.reduce((total, r) => total + r[column], 0);
const volume = (r: RptRow) => Math.abs(r.sales) + Math.abs(r.purchases) + Math.abs(r.received) + Math.abs(r.paid) + Math.abs(r.journals);

export default function RelatedParties() {
  const { period, setPeriod, query } = useShell();
  const [params, setParams] = useSearchParams();
  const company = params.get("company") || undefined;
  const { data, error, loading } = useLoad(() => getRpt(period, company), `${query}|${company}`);

  const setCompany = (value: string) => {
    const next = new URLSearchParams(params);
    if (value) next.set("company", value);
    else next.delete("company");
    setParams(next, { replace: true });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Related party transactions</h1>
          <p className="mt-0.5 text-sm text-ink-3">
            {data ? `${date(data.from_date)} to ${date(data.to_date)}` : "For each group company, with each of its related parties"}
            {" · Ind AS 24, Companies Act section 188"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <select className="field" value={company ?? ""} onChange={(e) => setCompany(e.target.value)} aria-label="Company">
            <option value="">All group companies</option>
            {data?.companies.map((c) => <option key={c.pan} value={c.pan}>{c.name}</option>)}
          </select>
          <PeriodFilter period={period} onChange={setPeriod} />
          <a className="btn" href="/app/accounts-intelligence-related-party">
            <Icon name="users" /> Register
          </a>
          <button className="btn-primary" disabled={!data?.rows.length} onClick={() => data && download(data.rows, data.from_date, data.to_date)}>
            <Icon name="download" /> CSV
          </button>
        </div>
      </div>

      {error && <Message tone="error">{error}</Message>}
      {!error && loading && !data && <Message>Loading…</Message>}
      {data && data.register_size === 0 && (
        <div className="card flex flex-wrap items-center justify-between gap-3 p-4 text-sm">
          <span className="text-ink-2">
            Only group companies are listed. Add directors, key managerial personnel, their relatives and the firms they
            control to the register, by PAN, to include them.
          </span>
          <a className="btn" href="/app/accounts-intelligence-related-party/new">Add a related party</a>
        </div>
      )}
      {data && data.rows.length === 0 && <Message>No transactions with related parties in this period.</Message>}
      {data && data.rows.length > 0 && (
        <div className={`space-y-5 transition-opacity ${loading ? "opacity-60" : ""}`}>
          <Totals rows={data.rows} />
          <ByRelationship rows={data.rows} order={data.relationships} />
          <Table rows={data.rows} order={data.relationships} showCompany={!company} />
          <p className="text-xs text-ink-3">
            Amounts in company currency from the synced documents: sales and purchases without tax and net of returns,
            money received and paid through payment entries, journal entries as the net debit to the party, and the
            balance on the last day of the period. Remuneration paid through payroll is not synced, so it is not included.
          </p>
        </div>
      )}
    </div>
  );
}

function Totals({ rows }: { rows: RptRow[] }) {
  const parties = new Set(rows.map((r) => r.party.pan)).size;
  const receivable = rows.reduce((t, r) => t + Math.max(r.balance ?? 0, 0), 0);
  const payable = rows.reduce((t, r) => t + Math.max(-(r.balance ?? 0), 0), 0);
  const cards: [string, string, string, string?][] = [
    ["Related parties", String(parties), `${rows.length} company–party ${rows.length === 1 ? "line" : "lines"}`],
    ["Sales to them", moneyShort(sum(rows, "sales")), "without tax", money(sum(rows, "sales"))],
    ["Purchases from them", moneyShort(sum(rows, "purchases")), "without tax", money(sum(rows, "purchases"))],
    ["Outstanding", moneyShort(receivable - payable), `receivable ${moneyShort(receivable)} · payable ${moneyShort(payable)}`],
  ];
  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {cards.map(([label, value, caption, title]) => (
        <div key={label} className="card p-4">
          <span className="caps">{label}</span>
          <div className="mt-2 text-2xl font-semibold tracking-tight tabular-nums" title={title}>{value}</div>
          <div className="mt-0.5 text-xs text-ink-3">{caption}</div>
        </div>
      ))}
    </div>
  );
}

/** How much business each kind of relationship accounts for. */
function ByRelationship({ rows, order }: { rows: RptRow[]; order: string[] }) {
  const groups = order
    .map((name) => ({ name, total: rows.filter((r) => r.party.relationship === name).reduce((t, r) => t + volume(r), 0) }))
    .filter((g) => g.total > 0);
  const max = Math.max(...groups.map((g) => g.total), 1);
  if (!groups.length) return null;
  return (
    <section className="card p-4">
      <h2 className="font-semibold tracking-tight">Business by relationship</h2>
      <p className="mb-4 text-xs text-ink-3">Sales, purchases, money and journals together</p>
      <div className="space-y-3">
        {groups.map((g) => (
          <div key={g.name}>
            <div className="mb-1 flex justify-between gap-3 text-sm">
              <span className="truncate">{g.name}</span>
              <span className="font-semibold tabular-nums" title={money(g.total)}>{moneyShort(g.total)}</span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-sunken">
              <div className="h-2 rounded-full bg-accent" style={{ width: `${Math.max((g.total / max) * 100, 1.5)}%` }} />
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Table({ rows, order, showCompany }: { rows: RptRow[]; order: string[]; showCompany: boolean }) {
  const groups = order.map((name) => ({ name, rows: rows.filter((r) => r.party.relationship === name) })).filter((g) => g.rows.length);
  const span = showCompany ? 2 : 1;
  return (
    <section className="card overflow-hidden">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[900px] text-sm">
          <thead className="bg-sunken/60 text-left">
            <tr className="caps">
              <th className="px-4 py-2.5 font-semibold">Related party</th>
              {showCompany && <th className="px-4 py-2.5 font-semibold">Company</th>}
              {COLUMNS.map(([, label]) => <th key={label} className="px-4 py-2.5 text-right font-semibold">{label}</th>)}
              <th className="px-4 py-2.5 text-right font-semibold">Balance</th>
            </tr>
          </thead>
          <tbody>
            {groups.map((group) => (
              <Fragment key={group.name}>
                <tr className="border-t border-line bg-sunken/30">
                  <td colSpan={span + COLUMNS.length + 1} className="px-4 py-2 text-xs font-semibold text-ink-2">{group.name}</td>
                </tr>
                {group.rows.map((r) => (
                  <tr key={r.company.pan + r.party.pan} className="border-t border-line hover:bg-sunken/40">
                    <td className="px-4 py-2.5">
                      <div className="font-medium">{r.party.name}</div>
                      <div className="text-xs tabular-nums text-ink-3">
                        {r.party.pan}
                        {(r.party.related_from || r.party.related_until) &&
                          ` · related ${r.party.related_from ? `from ${date(r.party.related_from)}` : ""}${r.party.related_until ? ` until ${date(r.party.related_until)}` : ""}`}
                      </div>
                    </td>
                    {showCompany && <td className="px-4 py-2.5 text-ink-2" title={r.company.name}>{acronym(r.company.name)}</td>}
                    {COLUMNS.map(([key]) => <Amount key={key} value={r[key]} />)}
                    <td className="px-4 py-2.5 text-right">
                      <Balance value={r.balance} />
                    </td>
                  </tr>
                ))}
                {group.rows.length > 1 && (
                  <tr className="border-t border-line text-ink-2">
                    <td colSpan={span} className="px-4 py-2 text-xs font-medium">Total {group.name.toLowerCase()}</td>
                    {COLUMNS.map(([key]) => <Amount key={key} value={sum(group.rows, key)} strong />)}
                    <td />
                  </tr>
                )}
              </Fragment>
            ))}
            <tr className="border-t-2 border-line bg-sunken/40">
              <td colSpan={span} className="px-4 py-2.5 font-semibold">All related parties</td>
              {COLUMNS.map(([key]) => <Amount key={key} value={sum(rows, key)} strong />)}
              <td />
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  );
}

function Amount({ value, strong }: { value: number; strong?: boolean }) {
  return (
    <td className={`whitespace-nowrap px-4 py-2.5 text-right tabular-nums ${value ? "" : "text-ink-3"} ${strong ? "font-semibold" : ""}`}>
      {value ? money(value) : "—"}
    </td>
  );
}

function Balance({ value }: { value: number | null }): ReactNode {
  if (value == null) return <span className="text-ink-3">—</span>;
  if (Math.abs(value) < 0.005) return <span className="text-ink-3">Settled</span>;
  return (
    <>
      <div className="whitespace-nowrap font-medium tabular-nums">{money(Math.abs(value))}</div>
      <div className="text-[11px] text-ink-3">{value > 0 ? "receivable" : "payable"}</div>
    </>
  );
}

function download(rows: RptRow[], from: string, to: string) {
  downloadCsv(`related-party-transactions-${from}-${to}.csv`, [
    ["Company", "Company PAN", "Related party", "PAN", "Relationship", ...COLUMNS.map(([, label]) => label), "Balance", "Documents"],
    ...rows.map((r) => [
      r.company.name, r.company.pan, r.party.name, r.party.pan, r.party.relationship,
      ...COLUMNS.map(([key]) => r[key]), r.balance, r.documents,
    ]),
  ]);
}

import type { ReactNode } from "react";
import { Link, useNavigate } from "react-router-dom";
import type { Counts, PairRow } from "../api";
import { AgeingBars, GapBars, MonthBars, Network, Ring } from "../components/Charts";
import Icon, { type IconName } from "../components/Icon";
import { TONE, TONE_TEXT, acronym, ago, gapTone, money, moneyShort, short } from "../format";
import { Message, PeriodFilter, downloadCsv, useShell } from "../Layout";

const open = (c: Counts) => c.amount_differs + c.date_differs + c.only_a + c.only_b;

export default function Dashboard() {
  const { dashboard, period, setPeriod, query } = useShell();
  const { data, error, loading } = dashboard;

  const pairs = data?.pairs ?? [];
  const companies = new Set(pairs.flatMap((p) => [p.a.pan, p.b.pan])).size;

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Group reconciliation</h1>
          <p className="mt-0.5 text-sm text-ink-3">
            {data
              ? `${companies} companies · ${pairs.length} ${pairs.length === 1 ? "pair" : "pairs"} · last sync ${ago(data.sync.last_sync_on)}`
              : "What each pair of group companies recorded about each other"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <PeriodFilter period={period} onChange={setPeriod} />
          <button className="btn-primary" disabled={!pairs.length} onClick={() => downloadPairs(pairs)}>
            <Icon name="download" /> Report
          </button>
        </div>
      </div>

      {error && <Message tone="error">{error}</Message>}
      {!error && loading && !data && <Message>Loading…</Message>}
      {data && pairs.length === 0 && (
        <Message>
          No data yet. Company sites send their documents once <b>Send Data to Server</b> is on in their settings.
        </Message>
      )}
      {data && pairs.length > 0 && (
        <div className={`space-y-5 transition-opacity ${loading ? "opacity-60" : ""}`}>
          <Kpis pairs={pairs} />

          <div className="grid gap-5 md:grid-cols-2">
            <Panel title="Balance gap by company pair" note="What the two books disagree on today">
              <GapBars pairs={pairs} query={query} />
            </Panel>
            <Panel title="Lines by month" legend>
              <MonthBars months={data.months} />
            </Panel>
            <Panel title="Ageing of open items" note="Days since the document date, with the amount at stake">
              <AgeingBars ageing={data.ageing} />
            </Panel>
            <Panel title="Group network" note="Click a link to open that pair">
              <Network pairs={pairs} query={query} />
            </Panel>
          </div>

          <PairsTable pairs={pairs} query={query} />
        </div>
      )}
    </div>
  );
}

function Kpis({ pairs }: { pairs: PairRow[] }) {
  const gap = pairs.reduce((sum, p) => sum + Math.abs(p.summary.balance_difference), 0);
  const worst = [...pairs].sort((x, y) => Math.abs(y.summary.balance_difference) - Math.abs(x.summary.balance_difference))[0];
  const withGap = pairs.filter((p) => gapTone(p.summary.balance_difference, p.summary.open) !== "good").length;
  const openInvoices = pairs.reduce((sum, p) => sum + open(p.summary.invoice), 0);
  const openMoney = pairs.reduce((sum, p) => sum + open(p.summary.money), 0);
  const matched = pairs.reduce((sum, p) => sum + p.summary.invoice.matched + p.summary.money.matched, 0);
  const total = matched + openInvoices + openMoney;
  const rate = total ? Math.round((matched / total) * 1000) / 10 : 100;
  const unexplained = pairs.some((p) => p.summary.unexplained == null)
    ? null
    : pairs.reduce((sum, p) => sum + Math.abs(p.summary.unexplained ?? 0), 0);

  return (
    <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
      <Kpi label="Balance gap" icon="alert" tone={gap > 1 ? "bad" : "good"} value={moneyShort(gap)} title={money(gap)}
        caption={`across ${withGap} of ${pairs.length} company ${pairs.length === 1 ? "pair" : "pairs"}`}
        footer={["Largest", worst ? `${acronym(worst.a.name)} ↔ ${acronym(worst.b.name)}` : "—"]} />
      <Kpi label="Open items" icon="pair" value={String(openInvoices + openMoney)}
        caption="lines that need a look"
        footer={["Invoices · money", `${openInvoices} · ${openMoney}`]} />
      <Kpi label="Match rate" icon="check" value={`${rate}%`} caption={`${matched} of ${total} lines`}
        aside={<Ring value={rate} />} footer={["Matched automatically", String(matched)]} />
      <Kpi label="Unexplained" icon="check" tone={unexplained == null ? undefined : unexplained > 1 ? "warn" : "good"}
        value={unexplained == null ? "—" : moneyShort(unexplained)} title={unexplained == null ? undefined : money(unexplained)}
        caption={unexplained == null ? "shown for all dates only" : "gap no open line accounts for"}
        footer={["Status", unexplained == null ? "Pick all time" : unexplained > 1 ? "Check sync start date" : "Fully explained"]} />
    </div>
  );
}

function Kpi({ label, icon, value, caption, footer, tone, title, aside }: {
  label: string; icon: IconName; value: string; caption: string; footer: [string, string];
  tone?: keyof typeof TONE; title?: string; aside?: ReactNode;
}) {
  return (
    <div className="card flex flex-col justify-between p-4">
      <div className="flex items-center justify-between">
        <span className="caps">{label}</span>
        <Icon name={icon} className={`h-4 w-4 ${tone ? TONE_TEXT[tone] : "text-ink-3"}`} />
      </div>
      <div className="mt-2 flex items-center justify-between gap-2">
        <div>
          <div className={`text-2xl font-semibold tracking-tight tabular-nums ${tone ? TONE_TEXT[tone] : ""}`} title={title}>{value}</div>
          <div className="mt-0.5 text-xs text-ink-3">{caption}</div>
        </div>
        {aside}
      </div>
      <div className="mt-3 flex items-center justify-between gap-2 rounded-md bg-sunken px-2.5 py-1.5">
        <span className="caps">{footer[0]}</span>
        <span className="truncate text-xs font-medium tabular-nums">{footer[1]}</span>
      </div>
    </div>
  );
}

function Panel({ title, note, legend, children }: { title: string; note?: string; legend?: boolean; children: ReactNode }) {
  return (
    <section className="card p-4">
      <div className="mb-4 flex flex-wrap items-start justify-between gap-2">
        <div>
          <h2 className="font-semibold tracking-tight">{title}</h2>
          {note && <p className="text-xs text-ink-3">{note}</p>}
        </div>
        {legend && (
          <div className="flex items-center gap-3 text-[11px] text-ink-2">
            {[["bg-emerald-500", "Matched"], ["bg-amber-500", "Differs"], ["bg-rose-500", "Missing"]].map(([color, name]) => (
              <span key={name} className="flex items-center gap-1"><span className={`h-2 w-2 rounded-full ${color}`} />{name}</span>
            ))}
          </div>
        )}
      </div>
      {children}
    </section>
  );
}

function CountPills({ counts }: { counts: Counts }) {
  const issues = open(counts);
  return (
    <div className="flex flex-wrap gap-1.5">
      <span className={`rounded-md px-2 py-0.5 text-xs font-medium tabular-nums ring-1 ring-inset ${TONE.good}`}>{counts.matched} matched</span>
      {issues > 0 && <span className={`rounded-md px-2 py-0.5 text-xs font-semibold tabular-nums ring-1 ring-inset ${TONE.bad}`}>{issues} open</span>}
    </div>
  );
}

function PairsTable({ pairs, query }: { pairs: PairRow[]; query: string }) {
  const navigate = useNavigate();
  const rows = [...pairs].sort((x, y) => Math.abs(y.summary.balance_difference) - Math.abs(x.summary.balance_difference));
  return (
    <section className="card overflow-hidden">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <div>
          <h2 className="font-semibold tracking-tight">Company pairs</h2>
          <p className="text-xs text-ink-3">Largest gap first</p>
        </div>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] text-sm">
          <thead className="bg-sunken/60 text-left">
            <tr className="caps">
              <th className="px-4 py-2.5 font-semibold">Companies</th>
              <th className="px-4 py-2.5 font-semibold">Invoices</th>
              <th className="px-4 py-2.5 font-semibold">Payments &amp; journals</th>
              <th className="px-4 py-2.5 text-right font-semibold">Balance gap</th>
              <th className="px-4 py-2.5 text-right font-semibold">Unexplained</th>
              <th className="w-10" />
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map(({ a, b, summary }) => {
              const to = `/pair/${a.pan}/${b.pan}${query}`;
              const tone = gapTone(summary.balance_difference, summary.open);
              return (
                <tr key={a.pan + b.pan} className="group cursor-pointer hover:bg-sunken/60" onClick={() => navigate(to)}>
                  <td className="px-4 py-3">
                    <Link to={to} className="font-medium group-hover:text-accent" onClick={(e) => e.stopPropagation()}>
                      {short(a.name)} ↔ {short(b.name)}
                    </Link>
                    <div className="text-xs tabular-nums text-ink-3">{a.pan} · {b.pan}</div>
                  </td>
                  <td className="px-4 py-3"><CountPills counts={summary.invoice} /></td>
                  <td className="px-4 py-3"><CountPills counts={summary.money} /></td>
                  <td className={`px-4 py-3 text-right font-semibold tabular-nums ${TONE_TEXT[tone]}`}>{money(summary.balance_difference)}</td>
                  <td className="px-4 py-3 text-right tabular-nums text-ink-2">
                    {summary.unexplained == null ? <span title="Shown for all dates only">—</span> : money(summary.unexplained)}
                  </td>
                  <td className="pr-3 text-ink-3 group-hover:text-accent"><Icon name="chevronRight" /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}

function downloadPairs(pairs: PairRow[]) {
  downloadCsv("group-reconciliation.csv", [
    ["Company A", "PAN A", "Company B", "PAN B", "Invoices matched", "Invoices open", "Money matched", "Money open",
      "Balance difference", "Unexplained"],
    ...pairs.map(({ a, b, summary: s }) => [
      a.name, a.pan, b.name, b.pan, s.invoice.matched, open(s.invoice), s.money.matched, open(s.money),
      s.balance_difference, s.unexplained,
    ]),
  ]);
}

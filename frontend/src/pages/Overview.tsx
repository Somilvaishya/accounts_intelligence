import { Link, useNavigate } from "react-router-dom";
import { getOverview, useLoad, type Counts } from "../api";
import { money, short } from "../format";
import { Message, PeriodFilter, usePeriod } from "../Layout";

const open = (c: Counts) => c.amount_differs + c.date_differs + c.only_a + c.only_b;

export default function Overview() {
  const [period, setPeriod, query] = usePeriod();
  const { data, error, loading } = useLoad(() => getOverview(period), query);
  const navigate = useNavigate();

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold tracking-tight">Group reconciliation</h1>
          <p className="text-sm text-slate-500">What each pair of group companies recorded about each other.</p>
        </div>
        <PeriodFilter period={period} onChange={setPeriod} />
      </div>

      {error && <Message tone="error">{error}</Message>}
      {!error && loading && !data && <Message>Loading…</Message>}
      {data && data.length === 0 && (
        <Message>
          No data yet. Company sites send their documents once <b>Send Data to Server</b> is on in their settings.
        </Message>
      )}
      {data && data.length > 0 && (
        <div className={`overflow-x-auto rounded-lg border border-slate-200 bg-white ${loading ? "opacity-60" : ""}`}>
          <table className="w-full min-w-[760px] text-sm">
            <thead className="border-b border-slate-200 bg-slate-50 text-left text-xs uppercase tracking-wide text-slate-500">
              <tr>
                <th className="px-4 py-2.5 font-medium">Companies</th>
                <th className="px-4 py-2.5 text-right font-medium">Invoices</th>
                <th className="px-4 py-2.5 text-right font-medium">Payments &amp; journals</th>
                <th className="px-4 py-2.5 text-right font-medium">Balance difference</th>
                <th className="px-4 py-2.5 text-right font-medium" title="Balance difference no synced document explains">
                  Unexplained
                </th>
              </tr>
            </thead>
            <tbody className="divide-y divide-slate-100">
              {data.map(({ a, b, summary }) => {
                const to = `/pair/${a.pan}/${b.pan}${query}`;
                return (
                  <tr key={`${a.pan}${b.pan}`} className="cursor-pointer hover:bg-slate-50" onClick={() => navigate(to)}>
                    <td className="px-4 py-3">
                      <Link to={to} className="font-medium text-slate-900 hover:underline" onClick={(e) => e.stopPropagation()}>
                        {short(a.name)} ↔ {short(b.name)}
                      </Link>
                      <div className="text-xs text-slate-400">{a.pan} · {b.pan}</div>
                    </td>
                    <CountCell counts={summary.invoice} />
                    <CountCell counts={summary.money} />
                    <td className={`px-4 py-3 text-right tabular-nums ${Math.abs(summary.balance_difference) > 1 ? "text-rose-700" : ""}`}>
                      {money(summary.balance_difference)}
                    </td>
                    <td className="px-4 py-3 text-right tabular-nums text-slate-600">
                      {summary.unexplained == null ? <span title="Only shown for all dates">—</span> : money(summary.unexplained)}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

function CountCell({ counts }: { counts: Counts }) {
  const issues = open(counts);
  return (
    <td className="px-4 py-3 text-right tabular-nums">
      <span className="text-emerald-700">{counts.matched} matched</span>
      {issues > 0 && <span className="ml-2 font-medium text-rose-700">{issues} open</span>}
    </td>
  );
}

// Small charts drawn with divs and SVG: the dashboard needs four simple shapes, not a chart library.
import type { ReactNode } from "react";
import { useNavigate } from "react-router-dom";
import type { AgeBucket, Month, PairRow } from "../api";
import { acronym, gapTone, money, moneyShort, monthName, short } from "../format";

const BAR = { good: "bg-emerald-500", warn: "bg-amber-500", bad: "bg-rose-500" };
const STROKE = { good: "#10B981", warn: "#F59E0B", bad: "#EF4444" };

export function Empty({ children }: { children: ReactNode }) {
  return <div className="flex h-32 items-center justify-center text-sm text-ink-3">{children}</div>;
}

/** Balance gap per company pair, largest first. */
export function GapBars({ pairs, query }: { pairs: PairRow[]; query: string }) {
  const navigate = useNavigate();
  const rows = [...pairs].sort((x, y) => Math.abs(y.summary.balance_difference) - Math.abs(x.summary.balance_difference));
  const max = Math.max(...rows.map((p) => Math.abs(p.summary.balance_difference)), 1);
  if (!rows.length) return <Empty>No company pairs yet</Empty>;
  return (
    <div className="space-y-3.5">
      {rows.map(({ a, b, summary }) => {
        const tone = gapTone(summary.balance_difference, summary.open);
        const width = Math.max((Math.abs(summary.balance_difference) / max) * 100, 1.5);
        return (
          <button key={a.pan + b.pan} className="block w-full text-left" onClick={() => navigate(`/pair/${a.pan}/${b.pan}${query}`)}>
            <div className="mb-1 flex items-center justify-between gap-2 text-sm">
              <span className="truncate font-medium hover:text-accent">{acronym(a.name)} ↔ {acronym(b.name)}</span>
              <span className="tabular-nums font-semibold" title={money(summary.balance_difference)}>
                {moneyShort(Math.abs(summary.balance_difference))}
              </span>
            </div>
            <div className="h-2 overflow-hidden rounded-full bg-sunken">
              <div className={`h-2 rounded-full ${BAR[tone]}`} style={{ width: `${width}%` }} />
            </div>
          </button>
        );
      })}
    </div>
  );
}

/** Lines per month, stacked by outcome: the last twelve months that have lines. */
export function MonthBars({ months }: { months: Month[] }) {
  const shown = months.slice(-12);
  const max = Math.max(...shown.map((m) => m.matched + m.differs + m.missing), 1);
  if (!shown.length) return <Empty>No documents in this period</Empty>;
  return (
    <div>
      <div className="flex h-36 items-end gap-1.5">
        {shown.map((m) => {
          const total = m.matched + m.differs + m.missing;
          const part = (n: number) => `${(n / max) * 100}%`;
          return (
            <div key={m.month} className="flex h-full flex-1 flex-col justify-end"
              title={`${monthName(m.month)} ${m.month.slice(0, 4)}: ${m.matched} matched, ${m.differs} differ, ${m.missing} missing`}>
              {total > 0 && (
                <div className="flex flex-col overflow-hidden rounded-[3px]" style={{ height: part(total) }}>
                  <div className="bg-rose-500" style={{ flexGrow: m.missing }} />
                  <div className="bg-amber-500" style={{ flexGrow: m.differs }} />
                  <div className="bg-emerald-500" style={{ flexGrow: m.matched }} />
                </div>
              )}
            </div>
          );
        })}
      </div>
      <div className="mt-1.5 flex gap-1.5 border-t border-line pt-1.5">
        {shown.map((m) => (
          <span key={m.month} className="flex-1 text-center text-[10px] text-ink-3">{monthName(m.month)}</span>
        ))}
      </div>
    </div>
  );
}

const AGE_TONE = ["bg-emerald-500", "bg-emerald-500", "bg-amber-500", "bg-rose-500"];

/** Open lines by age, with the total difference they carry. */
export function AgeingBars({ ageing }: { ageing: AgeBucket[] }) {
  const max = Math.max(...ageing.map((b) => b.amount), 1);
  return (
    <div className="grid grid-cols-4 gap-2">
      {ageing.map((b, i) => (
        <div key={b.bucket} className="flex flex-col items-center text-center">
          <span className={`text-xs font-semibold tabular-nums ${b.count ? "" : "text-ink-3"}`} title={money(b.amount)}>
            {moneyShort(b.amount)}
          </span>
          <div className="mt-1.5 flex h-24 w-full items-end rounded-lg bg-sunken p-1">
            {b.count > 0 && <div className={`w-full rounded ${AGE_TONE[i]}`} style={{ height: `${Math.max((b.amount / max) * 100, 4)}%` }} />}
          </div>
          <span className="mt-1.5 text-[11px] font-medium text-ink-2">{b.bucket.replace(" days", "d")}</span>
          <span className="text-[11px] text-ink-3">{b.count} {b.count === 1 ? "item" : "items"}</span>
        </div>
      ))}
    </div>
  );
}

/** The group as a graph: companies as nodes, pairs as edges coloured by their gap. */
export function Network({ pairs, query }: { pairs: PairRow[]; query: string }) {
  const navigate = useNavigate();
  const companies = new Map<string, string>();
  for (const { a, b } of pairs) {
    companies.set(a.pan, a.name);
    companies.set(b.pan, b.name);
  }
  if (!pairs.length) return <Empty>No company pairs yet</Empty>;

  const width = 340;
  const height = 190;
  const ids = [...companies.keys()];
  const at = new Map(
    ids.map((pan, i) => {
      const angle = -Math.PI / 2 + (2 * Math.PI * i) / ids.length + (ids.length === 2 ? Math.PI / 2 : 0);
      return [pan, { x: width / 2 + 120 * Math.cos(angle), y: height / 2 + 66 * Math.sin(angle) }];
    }),
  );

  return (
    <svg viewBox={`0 0 ${width} ${height}`} className="h-48 w-full" role="img" aria-label="Group companies and the gaps between them">
      {pairs.map(({ a, b, summary }) => {
        const p = at.get(a.pan)!;
        const q = at.get(b.pan)!;
        const tone = gapTone(summary.balance_difference, summary.open);
        const label = tone === "good" ? "✓ matched" : moneyShort(Math.abs(summary.balance_difference));
        const mx = (p.x + q.x) / 2;
        const my = (p.y + q.y) / 2;
        const w = label.length * 5.6 + 12;
        return (
          <g key={a.pan + b.pan} className="cursor-pointer" onClick={() => navigate(`/pair/${a.pan}/${b.pan}${query}`)}>
            <title>{`${short(a.name)} ↔ ${short(b.name)}: ${money(summary.balance_difference)}`}</title>
            <line x1={p.x} y1={p.y} x2={q.x} y2={q.y} stroke={STROKE[tone]} strokeWidth={2.5}
              strokeDasharray={tone === "bad" ? "5 3" : undefined} />
            <rect x={mx - w / 2} y={my - 9} width={w} height={18} rx={5} className="fill-surface" stroke={STROKE[tone]} />
            <text x={mx} y={my + 3.5} textAnchor="middle" fontSize={10} fontWeight={600} fill={STROKE[tone]}>{label}</text>
          </g>
        );
      })}
      {ids.map((pan) => {
        const { x, y } = at.get(pan)!;
        const name = companies.get(pan)!;
        return (
          <g key={pan}>
            <title>{`${name} · ${pan}`}</title>
            <circle cx={x} cy={y} r={22} className="fill-surface stroke-accent" strokeWidth={2} />
            <text x={x} y={y + 3.5} textAnchor="middle" fontSize={acronym(name).length > 4 ? 8.5 : 10} fontWeight={700}
              className="fill-ink">{acronym(name)}</text>
          </g>
        );
      })}
    </svg>
  );
}

/** A small progress ring for a percentage. */
export function Ring({ value }: { value: number }) {
  const r = 15;
  const length = 2 * Math.PI * r;
  return (
    <svg viewBox="0 0 36 36" className="h-11 w-11 -rotate-90">
      <circle cx={18} cy={18} r={r} fill="none" strokeWidth={3.5} className="stroke-sunken" />
      <circle cx={18} cy={18} r={r} fill="none" strokeWidth={3.5} strokeLinecap="round" stroke="#10B981"
        strokeDasharray={length} strokeDashoffset={length * (1 - value / 100)} />
    </svg>
  );
}

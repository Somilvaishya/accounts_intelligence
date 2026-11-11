import type { ReactNode } from "react";
import { Link, Outlet, useSearchParams } from "react-router-dom";
import type { Period } from "./api";

export default function Layout() {
  return (
    <div className="min-h-screen">
      <header className="border-b border-slate-200 bg-white">
        <div className="mx-auto flex max-w-7xl items-center justify-between gap-4 px-4 py-3">
          <Link to="/" className="font-semibold tracking-tight">
            Accounts Intelligence
          </Link>
          <a href="/app/accounts-intelligence-settings" className="text-sm text-slate-500 hover:text-slate-900">
            Settings
          </a>
        </div>
      </header>
      <main className="mx-auto max-w-7xl px-4 py-6">
        <Outlet />
      </main>
    </div>
  );
}

/** The period lives in the address (?from=…&to=…), so a filtered view can be bookmarked or shared. */
export function usePeriod(): [Period, (period: Period) => void, string] {
  const [params, setParams] = useSearchParams();
  const period = { from_date: params.get("from") || undefined, to_date: params.get("to") || undefined };
  const set = (next: Period) => {
    const updated = new URLSearchParams(params);
    for (const [key, value] of [["from", next.from_date], ["to", next.to_date]] as const) {
      if (value) updated.set(key, value);
      else updated.delete(key);
    }
    setParams(updated, { replace: true });
  };
  // The period alone, for links to another page.
  const kept = new URLSearchParams();
  if (period.from_date) kept.set("from", period.from_date);
  if (period.to_date) kept.set("to", period.to_date);
  return [period, set, kept.toString() ? `?${kept}` : ""];
}

export function PeriodFilter({ period, onChange }: { period: Period; onChange: (period: Period) => void }) {
  const input = "rounded-md border border-slate-300 bg-white px-2 py-1.5 text-sm";
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-slate-600">
      <label className="flex items-center gap-1.5">
        From
        <input type="date" className={input} value={period.from_date ?? ""}
          onChange={(e) => onChange({ ...period, from_date: e.target.value || undefined })} />
      </label>
      <label className="flex items-center gap-1.5">
        To
        <input type="date" className={input} value={period.to_date ?? ""}
          onChange={(e) => onChange({ ...period, to_date: e.target.value || undefined })} />
      </label>
      {(period.from_date || period.to_date) && (
        <button className="text-slate-500 underline hover:text-slate-900" onClick={() => onChange({})}>
          All dates
        </button>
      )}
    </div>
  );
}

export function Message({ tone = "muted", children }: { tone?: "muted" | "error"; children: ReactNode }) {
  const style = tone === "error" ? "border-rose-200 bg-rose-50 text-rose-800" : "border-slate-200 bg-white text-slate-500";
  return <div className={`rounded-lg border px-4 py-6 text-center text-sm ${style}`}>{children}</div>;
}

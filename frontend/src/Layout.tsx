import { useEffect, useState, type ReactNode } from "react";
import { Link, NavLink, Outlet, useOutletContext, useSearchParams } from "react-router-dom";
import { getDashboard, useLoad, type Dashboard, type Period } from "./api";
import Chat from "./components/Chat";
import Icon from "./components/Icon";
import { acronym, ago, gapTone } from "./format";

export interface Shell {
  dashboard: { data?: Dashboard; error?: string; loading: boolean };
  period: Period;
  setPeriod: (period: Period) => void;
  /** The period alone as a query string, for links to another page. */
  query: string;
}

export const useShell = () => useOutletContext<Shell>();

const USER = document.getElementById("root")?.dataset.user || "";
const ASK = "ai-ask";

/** Open the assistant and ask it `question`, from anywhere on the page. */
export function askAssistant(question: string) {
  window.dispatchEvent(new CustomEvent(ASK, { detail: question }));
}

export default function Layout() {
  const [period, setPeriod, query] = usePeriod();
  const dashboard = useLoad(() => getDashboard(period), query);
  const [menu, setMenu] = useState(false);
  const [chatColumn, setChatColumn] = useState(true);
  const [chatDrawer, setChatDrawer] = useState(false);
  const [dark, setDark] = useState(() => document.documentElement.classList.contains("dark"));
  const [pending, setPending] = useState<string | null>(null);

  useEffect(() => {
    const onAsk = (e: Event) => {
      setPending((e as CustomEvent<string>).detail);
      if (matchMedia("(min-width: 1280px)").matches) setChatColumn(true);
      else setChatDrawer(true);
    };
    window.addEventListener(ASK, onAsk);
    return () => window.removeEventListener(ASK, onAsk);
  }, []);
  const chatProps = { pending, onPendingSent: () => setPending(null) };

  const toggleTheme = () => {
    const next = !dark;
    document.documentElement.classList.toggle("dark", next);
    try {
      localStorage.setItem("ai-theme", next ? "dark" : "light");
    } catch {
      // The choice simply is not remembered.
    }
    setDark(next);
  };
  const toggleChat = () => {
    if (matchMedia("(min-width: 1280px)").matches) setChatColumn((open) => !open);
    else setChatDrawer(true);
  };

  return (
    <div className="min-h-screen lg:pl-60">
      <Sidebar dashboard={dashboard.data} query={query} open={menu} onClose={() => setMenu(false)} />

      <header className="sticky top-0 z-30 flex h-14 items-center justify-between gap-3 border-b border-line bg-canvas/80 px-4 backdrop-blur-xl lg:px-6">
        <button className="rounded-md p-1.5 text-ink-2 hover:bg-sunken lg:hidden" onClick={() => setMenu(true)} aria-label="Menu">
          <Icon name="menu" className="h-5 w-5" />
        </button>
        <div className="hidden text-sm text-ink-3 lg:block">Inter-company reconciliation</div>
        <div className="flex items-center gap-2">
          <button className="rounded-md p-1.5 text-ink-2 hover:bg-sunken" onClick={toggleTheme} aria-label="Switch theme"
            title={dark ? "Light theme" : "Dark theme"}>
            <Icon name={dark ? "sun" : "moon"} />
          </button>
          <button className="btn" onClick={toggleChat}>
            <Icon name="sparkles" className="h-4 w-4 text-accent" />
            <span className="hidden sm:inline">Assistant</span>
          </button>
          {USER && (
            <div className="ml-1 flex items-center gap-2">
              <span className="flex h-8 w-8 items-center justify-center rounded-full bg-accent-soft text-xs font-semibold text-accent">
                {acronym(USER).slice(0, 2)}
              </span>
              <span className="hidden text-sm font-medium md:inline">{USER}</span>
            </div>
          )}
        </div>
      </header>

      <div className={`mx-auto grid max-w-[1600px] gap-5 p-4 lg:p-6 ${chatColumn ? "xl:grid-cols-[minmax(0,1fr)_360px]" : ""}`}>
        <main className="min-w-0">
          <Outlet context={{ dashboard, period, setPeriod, query } satisfies Shell} />
        </main>
        {chatColumn && (
          <div className="sticky top-20 hidden h-[calc(100vh-6.5rem)] xl:block">
            <Chat onClose={() => setChatColumn(false)} {...chatProps} />
          </div>
        )}
      </div>

      {chatDrawer && (
        <div className="fixed inset-0 z-50 xl:hidden">
          <div className="absolute inset-0 bg-slate-900/40 backdrop-blur-[2px]" onClick={() => setChatDrawer(false)} />
          <div className="absolute inset-y-0 right-0 w-full max-w-sm p-3">
            <Chat onClose={() => setChatDrawer(false)} {...chatProps} />
          </div>
        </div>
      )}
    </div>
  );
}

function Sidebar({ dashboard, query, open, onClose }: { dashboard?: Dashboard; query: string; open: boolean; onClose: () => void }) {
  const item = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-3 rounded-lg px-3 py-2 text-sm transition-colors ${
      isActive ? "bg-accent text-white" : "text-ink-2 hover:bg-sunken hover:text-ink"
    }`;
  const sync = dashboard?.sync;
  const toReview = dashboard?.pairs.reduce((n, p) => n + p.summary.open - p.summary.explained, 0) ?? 0;
  const syncTone = !sync?.sites ? "bg-slate-400" : sync.late.length ? "bg-amber-500" : "bg-emerald-500";

  return (
    <>
      {open && <div className="fixed inset-0 z-40 bg-slate-900/40 lg:hidden" onClick={onClose} />}
      <aside className={`fixed inset-y-0 left-0 z-50 flex w-60 flex-col border-r border-line bg-surface transition-transform lg:translate-x-0 ${
        open ? "translate-x-0" : "-translate-x-full"}`}>
        <Link to={`/${query}`} className="flex h-14 items-center gap-2.5 px-5" onClick={onClose}>
          <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-accent text-white">
            <Icon name="pair" className="h-4 w-4" />
          </span>
          <span className="leading-tight">
            <span className="block text-sm font-semibold">Accounts</span>
            <span className="caps block">Intelligence</span>
          </span>
        </Link>

        <nav className="flex flex-1 flex-col gap-1 overflow-y-auto px-3 py-2">
          <NavLink to={`/${query}`} end className={item} onClick={onClose}>
            <Icon name="grid" /> Dashboard
          </NavLink>
          <NavLink to={`/open-items${query}`} className={item} onClick={onClose}>
            <Icon name="alert" /> <span className="flex-1">Open items</span>
            {!!toReview && <span className="rounded-full bg-black/5 px-1.5 text-[11px] font-semibold tabular-nums dark:bg-white/10">{toReview}</span>}
          </NavLink>
          <NavLink to={`/related-parties${query}`} className={item} onClick={onClose}>
            <Icon name="users" /> Related parties
          </NavLink>
          <a href="/app/accounts-intelligence-settings" className={item({ isActive: false })}>
            <Icon name="settings" /> Settings
          </a>

          {!!dashboard?.pairs.length && <div className="caps mt-5 px-3 pb-1">Company pairs</div>}
          {dashboard?.pairs.map(({ a, b, summary }) => {
            const tone = gapTone(summary.balance_difference, summary.open);
            return (
              <NavLink key={a.pan + b.pan} to={`/pair/${a.pan}/${b.pan}${query}`} className={item} onClick={onClose}>
                <span className={`h-2 w-2 shrink-0 rounded-full ${{ good: "bg-emerald-500", warn: "bg-amber-500", bad: "bg-rose-500" }[tone]}`} />
                <span className="flex-1 truncate">{acronym(a.name)} ↔ {acronym(b.name)}</span>
                {summary.open > 0 && <span className="rounded-full bg-black/5 px-1.5 text-[11px] font-semibold tabular-nums dark:bg-white/10">{summary.open}</span>}
              </NavLink>
            );
          })}
        </nav>

        <div className="p-3">
          <div className="rounded-lg bg-sunken p-3">
            <div className="flex items-center justify-between">
              <span className="caps">Sync</span>
              <span className={`h-2 w-2 rounded-full ${syncTone}`} />
            </div>
            <p className="mt-1 text-xs text-ink-2">
              {!sync ? "…" : !sync.sites ? "No company site connected" : (
                <>
                  {sync.sites} company {sync.sites === 1 ? "site" : "sites"} · last sync {ago(sync.last_sync_on)}
                  {sync.late.length > 0 && <span className="block text-amber-700 dark:text-amber-400">{sync.late.length} late</span>}
                </>
              )}
            </p>
          </div>
        </div>
      </aside>
    </>
  );
}

/** The period lives in the address (?from=…&to=…), so a filtered view can be bookmarked or shared. */
function usePeriod(): [Period, (period: Period) => void, string] {
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
  const kept = new URLSearchParams();
  if (period.from_date) kept.set("from", period.from_date);
  if (period.to_date) kept.set("to", period.to_date);
  return [period, set, kept.toString() ? `?${kept}` : ""];
}

export function PeriodFilter({ period, onChange }: { period: Period; onChange: (period: Period) => void }) {
  return (
    <div className="flex flex-wrap items-center gap-2 text-sm text-ink-2">
      <input type="date" className="field" aria-label="From" value={period.from_date ?? ""}
        onChange={(e) => onChange({ ...period, from_date: e.target.value || undefined })} />
      <span className="text-ink-3">to</span>
      <input type="date" className="field" aria-label="To" value={period.to_date ?? ""}
        onChange={(e) => onChange({ ...period, to_date: e.target.value || undefined })} />
      {(period.from_date || period.to_date) && (
        <button className="text-xs text-ink-3 underline hover:text-ink" onClick={() => onChange({})}>All time</button>
      )}
    </div>
  );
}

export function Message({ tone = "muted", children }: { tone?: "muted" | "error"; children: ReactNode }) {
  const style = tone === "error"
    ? "border-rose-200 bg-rose-50 text-rose-800 dark:border-rose-500/30 dark:bg-rose-500/10 dark:text-rose-300"
    : "border-line bg-surface text-ink-3";
  return <div className={`rounded-xl border px-4 py-8 text-center text-sm ${style}`}>{children}</div>;
}

/** Turn rows into a CSV file and hand it to the browser. */
export function downloadCsv(name: string, rows: (string | number | null | undefined)[][]) {
  const cell = (value: string | number | null | undefined) => {
    const text = value == null ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  const blob = new Blob([rows.map((row) => row.map(cell).join(",")).join("\n")], { type: "text/csv" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = name;
  link.click();
  URL.revokeObjectURL(link.href);
}

import { getSyncHealth, useLoad, type SiteHealth, type SiteState } from "../api";
import Icon from "../components/Icon";
import { TONE, acronym, ago, date } from "../format";
import { Message } from "../Layout";

const STATE_TONE: Record<SiteState, string> = {
  Healthy: TONE.good,
  Late: TONE.warn,
  "Never synced": TONE.warn,
  Pending: "bg-sunken text-ink-2 ring-line",
  Disabled: "bg-sunken text-ink-2 ring-line",
  Revoked: TONE.bad,
};

const name = (site: SiteHealth) =>
  site.companies.length ? site.companies.map((c) => acronym(c.name)).join(", ") : site.site_url.replace(/^https?:\/\//, "");

export default function SyncHealth() {
  const { data, error, loading } = useLoad(getSyncHealth, "health");
  const sites = data?.sites ?? [];
  const healthy = sites.filter((s) => s.state === "Healthy").length;
  const late = sites.filter((s) => s.state === "Late" || s.state === "Never synced").length;
  const warnings = sites.flatMap((s) => s.warnings.map((text) => ({ site: s, text })));
  const rejected = sites.reduce((t, s) => t + s.rejected, 0);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Sync health</h1>
          <p className="mt-0.5 text-sm text-ink-3">
            Every company site that sends its books here{data ? ` · sites sync every ${data.sync_every} minutes` : ""}
          </p>
        </div>
        <a className="btn" href="/app/accounts-intelligence-settings">
          <Icon name="settings" /> Manage sites
        </a>
      </div>

      {error && <Message tone="error">{error}</Message>}
      {!error && loading && !data && <Message>Loading…</Message>}
      {data && sites.length === 0 && <Message>No company site is on the list yet. Add one in the settings.</Message>}
      {data && sites.length > 0 && (
        <>
          <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
            <Card label="Syncing" value={`${healthy} / ${sites.length}`} caption="sites synced on time" tone={healthy === sites.length ? "good" : undefined} />
            <Card label="Late" value={String(late)} caption="no sync for twice the interval" tone={late ? "warn" : "good"} />
            <Card label="Warnings" value={String(warnings.length)} caption="from the sites' last runs" tone={warnings.length ? "warn" : "good"} />
            <Card label="Refused calls" value={String(rejected)} caption="in the last 24 hours" tone={rejected ? "bad" : "good"} />
          </div>

          {warnings.length > 0 && (
            <section className="card p-4">
              <h2 className="font-semibold tracking-tight">Warnings</h2>
              <p className="mb-3 text-xs text-ink-3">Fix these in the company's own site; they clear on its next run</p>
              <ul className="space-y-2">
                {warnings.map(({ site, text }, i) => (
                  <li key={i} className="flex gap-2 text-sm">
                    <Icon name="alert" className="mt-0.5 h-4 w-4 shrink-0 text-amber-600 dark:text-amber-400" />
                    <span><span className="font-medium">{name(site)}:</span> <span className="text-ink-2">{text}</span></span>
                  </li>
                ))}
              </ul>
            </section>
          )}

          <section className="card overflow-hidden">
            <div className="overflow-x-auto">
              <table className="w-full min-w-[960px] text-sm">
                <thead className="bg-sunken/60 text-left">
                  <tr className="caps">
                    <th className="px-4 py-2.5 font-semibold">Company site</th>
                    <th className="px-4 py-2.5 font-semibold">State</th>
                    <th className="px-4 py-2.5 font-semibold">Last sync</th>
                    <th className="px-4 py-2.5 font-semibold">Daily check</th>
                    <th className="px-4 py-2.5 font-semibold">Last 24 hours</th>
                    <th className="px-4 py-2.5 text-right font-semibold">Documents here</th>
                  </tr>
                </thead>
                <tbody className="divide-y divide-line">
                  {sites.map((s) => <SiteRow key={s.site_url} site={s} />)}
                </tbody>
              </table>
            </div>
          </section>
        </>
      )}
    </div>
  );
}

function Card({ label, value, caption, tone }: { label: string; value: string; caption: string; tone?: keyof typeof TONE }) {
  const text = { good: "text-emerald-700 dark:text-emerald-400", warn: "text-amber-700 dark:text-amber-400", bad: "text-rose-700 dark:text-rose-400" };
  return (
    <div className="card p-4">
      <span className="caps">{label}</span>
      <div className={`mt-1.5 text-2xl font-semibold tracking-tight tabular-nums ${tone ? text[tone] : ""}`}>{value}</div>
      <div className="mt-0.5 text-xs text-ink-3">{caption}</div>
    </div>
  );
}

function SiteRow({ site }: { site: SiteHealth }) {
  const check = site.last_check?.replace(/^\d{4}-\d{2}-\d{2} \d{2}:\d{2}: /, "");
  const differs = !!site.last_check?.includes(" differ");
  return (
    <tr className="align-top hover:bg-sunken/40">
      <td className="px-4 py-3">
        <div className="font-medium">{site.companies.map((c) => c.name).join(", ") || "No company registered yet"}</div>
        <a href={site.site_url} target="_blank" rel="noreferrer" className="text-xs text-ink-3 hover:text-accent">{site.site_url}</a>
      </td>
      <td className="px-4 py-3">
        <span className={`inline-block whitespace-nowrap rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATE_TONE[site.state]}`}>
          {site.state}
        </span>
        {site.warnings.length > 0 && (
          <div className="mt-1 text-[11px] text-amber-700 dark:text-amber-400">
            {site.warnings.length} {site.warnings.length === 1 ? "warning" : "warnings"}
          </div>
        )}
      </td>
      <td className="whitespace-nowrap px-4 py-3">
        <div>{ago(site.last_sync_on)}</div>
        {site.last_sync_rows != null && <div className="text-xs text-ink-3">{site.last_sync_rows} rows in the last batch</div>}
      </td>
      <td className="max-w-[16rem] px-4 py-3">
        {check ? (
          <span className={`flex items-start gap-1.5 text-xs ${differs ? "text-amber-700 dark:text-amber-400" : "text-emerald-700 dark:text-emerald-400"}`}>
            <Icon name={differs ? "alert" : "check"} className="mt-px h-3.5 w-3.5 shrink-0" />
            <span className="line-clamp-2" title={site.last_check ?? undefined}>{check}</span>
          </span>
        ) : (
          <span className="text-xs text-ink-3">Not yet</span>
        )}
      </td>
      <td className="px-4 py-3">
        <Activity hours={site.activity} />
        {site.rejected > 0 && <div className="mt-1 text-[11px] text-rose-700 dark:text-rose-400">{site.rejected} refused</div>}
      </td>
      <td className="whitespace-nowrap px-4 py-3 text-right">
        <div className="tabular-nums">{site.documents.toLocaleString("en-IN")}</div>
        {site.latest_document && <div className="text-xs text-ink-3">latest {date(site.latest_document)}</div>}
      </td>
    </tr>
  );
}

/** Accepted sync calls per hour over the last day, oldest on the left. */
function Activity({ hours }: { hours: number[] }) {
  const max = Math.max(...hours, 1);
  const total = hours.reduce((t, n) => t + n, 0);
  return (
    <div className="flex h-7 w-36 items-end gap-px" title={`${total} sync calls in the last 24 hours`}>
      {hours.map((n, i) => (
        <div key={i} className={`flex-1 rounded-[1px] ${n ? "bg-accent" : "bg-sunken"}`} style={{ height: n ? `${Math.max((n / max) * 100, 15)}%` : "15%" }} />
      ))}
    </div>
  );
}

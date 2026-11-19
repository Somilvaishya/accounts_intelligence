import { useMemo, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { getInternalTransfers, useLoad, type InternalCompany, type InternalSummary, type Transfer, type TransferStatus } from "../api";
import Icon from "../components/Icon";
import { TONE, TONE_TEXT, acronym, ago, branch, date, docUrl, money, moneyShort, short } from "../format";
import { Message, downloadCsv } from "../Layout";

const STATUSES: { status: TransferStatus; tone: keyof typeof TONE }[] = [
  { status: "Not received", tone: "bad" },
  { status: "Amount differs", tone: "warn" },
  { status: "Check party", tone: "warn" },
  { status: "Not dispatched", tone: "bad" },
];
const STYLE = Object.fromEntries(STATUSES.map((s) => [s.status, TONE[s.tone]])) as Record<TransferStatus, string>;

const ageOf = (value: string) => Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 86400000));

export default function InternalTransfers() {
  const loaded = useLoad(getInternalTransfers, "internal");
  const [params, setParams] = useSearchParams();
  const [show, setShow] = useState<TransferStatus | "">("");
  const companies = loaded.data?.companies ?? [];
  const pan = companies.some((c) => c.company.pan === params.get("company")) ? params.get("company")! : companies[0]?.company.pan;
  const current = companies.find((c) => c.company.pan === pan);

  const items = useMemo(() => (loaded.data?.items ?? []).filter((i) => i.entity === pan), [loaded.data, pan]);
  const shown = show ? items.filter((i) => i.status === show) : items;

  const choose = (next: string) => {
    setShow("");
    // The period stays in the address, so the sidebar's links keep it.
    const updated = new URLSearchParams(params);
    updated.set("company", next);
    setParams(updated, { replace: true });
  };

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Internal transfers</h1>
          <p className="mt-0.5 text-sm text-ink-3">
            {current?.summary.received_on
              ? `Between a company's own branches · as of ${ago(current.summary.received_on)}`
              : "Goods one branch sends to another of the same company, and whether the other branch booked them"}
          </p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {companies.length > 1 && (
            <select className="field" value={pan} onChange={(e) => choose(e.target.value)} aria-label="Company">
              {companies.map(({ company }) => <option key={company.pan} value={company.pan}>{short(company.name)}</option>)}
            </select>
          )}
          <button className="btn-primary" disabled={!shown.length} onClick={() => current && download(current, shown)}>
            <Icon name="download" /> CSV
          </button>
        </div>
      </div>

      {loaded.error && <Message tone="error">{loaded.error}</Message>}
      {!loaded.error && loaded.loading && !loaded.data && <Message>Loading…</Message>}
      {loaded.data && !current && (
        <Message>
          No company site has sent internal transfers. They come from sites that use Business Needed Solutions for
          transfers between their branches.
        </Message>
      )}
      {current && (
        <>
          <Totals summary={current.summary} />
          <section className="card overflow-hidden">
            <div className="flex flex-wrap items-center justify-between gap-3 border-b border-line px-4 py-3">
              <div>
                <h2 className="font-semibold tracking-tight">To look at</h2>
                <p className="text-xs text-ink-3">
                  {current.summary.documents} transfers sent in all · only those not booked in full on both sides are listed
                </p>
              </div>
              <div className="flex flex-wrap gap-1.5">
                <Tab active={!show} onClick={() => setShow("")} label="All" count={items.length} />
                {STATUSES.map(({ status }) => [status, items.filter((i) => i.status === status).length] as const)
                  .filter(([, count]) => count > 0)
                  .map(([status, count]) => (
                    <Tab key={status} active={show === status} onClick={() => setShow(status)} label={status} count={count} />
                  ))}
              </div>
            </div>
            {shown.length === 0 ? (
              <div className="flex items-center justify-center gap-2 px-4 py-10 text-sm text-ink-3">
                <Icon name="check" className="h-4 w-4 text-emerald-600" /> Every transfer is booked on both sides.
              </div>
            ) : (
              <TransferTable items={shown} company={current} />
            )}
          </section>
          {current.summary.truncated && (
            <p className="text-xs text-ink-3">The site sent its oldest 500 transfers to look at; the totals above count all of them.</p>
          )}
        </>
      )}
    </div>
  );
}

function Tab({ active, onClick, label, count }: { active: boolean; onClick: () => void; label: string; count: number }) {
  return (
    <button onClick={onClick}
      className={`rounded-md px-2.5 py-1 text-xs font-medium transition-colors ${active ? "bg-accent text-white" : "bg-sunken text-ink-2 hover:text-ink"}`}>
      {label} <span className="tabular-nums opacity-70">{count}</span>
    </button>
  );
}

function Totals({ summary: s }: { summary: InternalSummary }) {
  const unexplained = s.unexplained;
  const otherwise = s.amount_differs.count + s.check_party.count + s.not_dispatched.count;
  const cards: { label: string; value: string; title?: string; caption: string; tone?: keyof typeof TONE }[] = [
    {
      label: "Not received", value: String(s.not_received.count), tone: s.not_received.count ? "bad" : "good",
      caption: s.not_received.count ? `${money(s.not_received.amount)} not booked by the receiving branch` : "every transfer was received",
    },
    {
      label: "Booked differently", value: String(otherwise), tone: otherwise ? "warn" : "good",
      caption: s.amount_differs.count
        ? `${money(s.amount_differs.amount)} between sent and received`
        : otherwise ? "a party or a sending document to check" : "amounts and parties agree",
    },
    {
      label: "Ledger gap", value: moneyShort(s.gap), title: money(s.gap),
      tone: s.gap == null ? undefined : Math.abs(s.gap) > 1 ? "warn" : "good",
      caption: s.gap == null ? "set the internal accounts in BNS settings" : "Internal Debtor + Internal Creditor",
    },
    {
      label: "Unexplained", value: moneyShort(unexplained), title: money(unexplained),
      tone: unexplained == null ? undefined : Math.abs(unexplained) > 1 ? "warn" : "good",
      caption: unexplained == null ? "needs the ledger gap" : Math.abs(unexplained) > 1 ? "gap no listed transfer accounts for" : "the listed transfers explain the gap",
    },
  ];
  return (
    <div className="grid grid-cols-2 gap-4 lg:grid-cols-4">
      {cards.map((c) => (
        <div key={c.label} className="card p-4">
          <span className="caps">{c.label}</span>
          <div className={`mt-1.5 text-2xl font-semibold tracking-tight tabular-nums ${c.tone ? TONE_TEXT[c.tone] : ""}`} title={c.title}>{c.value}</div>
          <div className="mt-0.5 text-xs text-ink-3">{c.caption}</div>
        </div>
      ))}
    </div>
  );
}

function TransferTable({ items, company }: { items: Transfer[]; company: InternalCompany }) {
  const site = company.company;
  const link = (doctype: string, name: string) => docUrl({ doctype, name, date: "", party: "", amount: 0 }, site);
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[820px] text-sm">
        <thead className="bg-sunken/60 text-left">
          <tr className="caps">
            <th className="px-4 py-2.5 font-semibold">Document</th>
            <th className="px-4 py-2.5 font-semibold">Branches</th>
            <th className="px-4 py-2.5 font-semibold">Status</th>
            <th className="px-4 py-2.5 text-right font-semibold">Sent</th>
            <th className="px-4 py-2.5 text-right font-semibold">Received</th>
            <th className="px-4 py-2.5 text-right font-semibold">Difference</th>
          </tr>
        </thead>
        <tbody className="divide-y divide-line">
          {items.map((t) => {
            const url = link(t.source_doctype, t.source_name);
            const age = ageOf(t.posting_date);
            const receiver = t.source_doctype === "Sales Invoice" ? "Purchase Invoice" : "Purchase Receipt";
            return (
              <tr key={t.source_doctype + t.source_name} className="align-top hover:bg-sunken/40">
                <td className="px-4 py-3">
                  {url ? (
                    <a href={url} target="_blank" rel="noreferrer" className="font-medium hover:text-accent">{t.source_name}</a>
                  ) : <span className="font-medium">{t.source_name}</span>}
                  <div className="text-xs text-ink-3">
                    {t.source_doctype} · {date(t.posting_date)} · <span className={age > 30 ? TONE_TEXT.warn : ""}>{age} days</span>
                  </div>
                </td>
                <td className="px-4 py-3 whitespace-nowrap">
                  <span title={`${t.from_gstin || "—"} → ${t.to_gstin || "—"}`}>{branch(t.from_gstin)} → {branch(t.to_gstin)}</span>
                  {t.from_gstin && t.from_gstin === t.to_gstin && <div className="text-xs text-ink-3">same GSTIN</div>}
                </td>
                <td className="px-4 py-3">
                  <span className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STYLE[t.status]}`}>{t.status}</span>
                  {(t.receivers || t.note) && (
                    <div className="mt-1 max-w-xs text-xs text-ink-3">
                      {t.receivers && (
                        <div>
                          {receiver}:{" "}
                          {t.receivers.split(", ").map((name, n) => {
                            const href = link(receiver, name);
                            return (
                              <span key={name}>
                                {n > 0 && ", "}
                                {href ? <a href={href} target="_blank" rel="noreferrer" className="hover:text-accent">{name}</a> : name}
                              </span>
                            );
                          })}
                        </div>
                      )}
                      {t.note && <div>{t.note}</div>}
                    </div>
                  )}
                </td>
                <td className="px-4 py-3 text-right tabular-nums">{t.sent ? money(t.sent) : "—"}</td>
                <td className="px-4 py-3 text-right tabular-nums">{t.received ? money(t.received) : "—"}</td>
                <td className="px-4 py-3 text-right font-semibold tabular-nums">{money(t.difference)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function download(company: InternalCompany, items: Transfer[]) {
  downloadCsv(`internal-transfers-${acronym(company.company.name)}.csv`, [
    ["Company", "Status", "Document type", "Document", "Date", "From GSTIN", "To GSTIN", "Party", "Sent", "Received",
      "Difference", "Receiving documents", "Note"],
    ...items.map((t) => [
      company.company.name, t.status, t.source_doctype, t.source_name, t.posting_date, t.from_gstin, t.to_gstin, t.party,
      t.sent, t.received, t.difference, t.receivers, t.note,
    ]),
  ]);
}

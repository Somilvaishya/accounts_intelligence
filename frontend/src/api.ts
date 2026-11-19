// The two read-only endpoints in accounts_intelligence/reconcile.py, and the shapes they return.
import { useEffect, useState } from "react";

export type Status = "matched" | "amount_differs" | "date_differs" | "only_a" | "only_b";
export type Counts = Record<Status, number>;

export interface Company {
  pan: string;
  name: string;
  site_url?: string | null;
}

export interface Doc {
  doctype: string;
  name: string;
  date: string;
  party: string;
  amount: number;
  bill_no?: string;
  payment_type?: string;
  reference_no?: string;
}

export type ReviewStatus = "Open" | "In progress" | "Explained";

export interface Review {
  status: ReviewStatus;
  assigned_to: string | null;
  reason: string | null;
  note: string | null;
}

/** Who reconciled a line by hand on the pair page, and when. */
export interface ManualMatch {
  name: string;
  by: string | null;
  by_name: string | null;
  on: string | null;
  note: string | null;
}

export interface Line {
  kind: "Invoice" | "Money";
  status: Status;
  date: string;
  /** The first document on each side; a manual match lists all of them in a_docs and b_docs. */
  a: Doc | null;
  b: Doc | null;
  a_docs?: Doc[];
  b_docs?: Doc[];
  match?: ManualMatch;
  difference: number;
  /** Stable across reloads: the review of an open line is saved under it. */
  key: string;
  review: Review | null;
}

export interface OpenItem extends Line {
  pair: { a: Company; b: Company };
  age: number;
}

export interface User {
  name: string;
  full_name: string;
}

export const REASONS = [
  "Timing difference",
  "Booked under another party",
  "Will be booked",
  "Disputed",
  "Not an inter-company transaction",
  "Other",
];

export interface Summary {
  invoice: Counts;
  money: Counts;
  open: number;
  explained: number;
  /** Matched lines someone reconciled by hand. */
  manual: number;
  balance_difference: number;
  unexplained: number | null;
}

export interface Balance {
  a: number | null;
  b: number | null;
  difference: number;
  as_of: string | null;
}

export interface Month {
  month: string;
  matched: number;
  differs: number;
  missing: number;
}

export interface AgeBucket {
  bucket: string;
  count: number;
  amount: number;
}

export interface Trend {
  months: Month[];
  ageing: AgeBucket[];
}

/** A match made by hand that no longer holds, such as one whose document was cancelled. */
export interface BrokenMatch extends ManualMatch {
  kind: Line["kind"];
  reason: string;
  documents: { entity: string; doctype: string; name: string }[];
}

export interface PairResult {
  a: Company;
  b: Company;
  summary: Summary;
  balance: Balance;
  trend: Trend;
  lines: Line[];
  broken_matches: BrokenMatch[];
}

export interface PairRow {
  a: Company;
  b: Company;
  summary: Summary;
}

export interface SyncHealth {
  sites: number;
  late: string[];
  last_sync_on: string | null;
  sync_every: number;
}

export interface Dashboard extends Trend {
  pairs: PairRow[];
  sync: SyncHealth;
  /** Companies whose site sent transfers between its own branches (BNS). */
  internal?: InternalCompany[];
}

export type TransferStatus = "Not received" | "Amount differs" | "Check party" | "Not dispatched";

export interface TransferTotals {
  count: number;
  amount: number;
}

/** One company's transfers between its own branches, as of its site's last sync. */
export interface InternalSummary {
  documents: number;
  sent: number;
  not_received: TransferTotals;
  amount_differs: TransferTotals;
  check_party: TransferTotals;
  not_dispatched: TransferTotals;
  debtor: number | null;
  creditor: number | null;
  debtor_account: string | null;
  creditor_account: string | null;
  /** Internal Debtor plus Internal Creditor: zero once every transfer is booked on both sides. */
  gap: number | null;
  explained: number | null;
  unexplained: number | null;
  truncated: boolean;
  received_on?: string;
}

export interface InternalCompany {
  company: Company;
  summary: InternalSummary;
}

export interface Transfer {
  entity: string;
  status: TransferStatus;
  kind: "Invoice" | "Delivery";
  source_doctype: string;
  source_name: string;
  posting_date: string;
  party: string;
  from_gstin: string;
  to_gstin: string;
  sent: number;
  received: number;
  difference: number;
  receivers: string;
  note: string;
}

export interface Period {
  from_date?: string;
  to_date?: string;
}

/** A line the assistant found, with its pair and a readable status. */
export interface FoundLine extends Line {
  pair: { a: Company; b: Company };
  status_label: string;
}

export interface Draft {
  subject: string;
  body: string;
}

export interface Answer {
  ok: boolean;
  answer: string;
  tool?: string;
  lines?: FoundLine[];
  draft?: Draft;
}

async function call<T>(method: string, params: Record<string, string | undefined>, post = false): Promise<T> {
  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) if (value) query.set(key, value);
  const url = `/api/method/accounts_intelligence.${method}`;
  const response = await fetch(post ? url : `${url}?${query}`, {
    method: post ? "POST" : "GET",
    headers: {
      Accept: "application/json",
      ...(post && { "Content-Type": "application/x-www-form-urlencoded", "X-Frappe-CSRF-Token": window.csrf_token ?? "" }),
    },
    body: post ? query : undefined,
    credentials: "same-origin",
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(serverMessage(body) || `The server answered ${response.status}.`);
  return body.message as T;
}

declare global {
  interface Window {
    csrf_token?: string;
  }
}

/** The message Frappe puts in _server_messages, or the exception's last line. */
function serverMessage(body: { _server_messages?: string; exception?: string }): string {
  try {
    const messages = JSON.parse(body._server_messages || "[]") as string[];
    const text = messages.map((m) => JSON.parse(m).message as string).join(" ");
    if (text) return text.replace(/<[^>]+>/g, "");
  } catch {
    // Not every error carries readable messages; fall back to the exception.
  }
  return body.exception?.split(":").slice(1).join(":").trim() || "";
}

export const getDashboard = (period: Period) => call<Dashboard>("reconcile.dashboard", { ...period });
export const getPair = (a: string, b: string, period: Period) => call<PairResult>("reconcile.pair", { a, b, ...period });
/** Reconcile open documents of both companies by hand. The server checks they are open and their totals agree. */
export const reconcileByHand = (a: string, b: string, documents: { entity: string; doctype: string; name: string }[], note: string) =>
  call<{ name: string }>("reconcile.match", { a, b, documents: JSON.stringify(documents), note }, true);
export const undoMatch = (name: string) => call<{ name: string }>("reconcile.unmatch", { name }, true);
export interface RptRow {
  company: { pan: string; name: string };
  party: { pan: string; name: string; relationship: string; related_from?: string | null; related_until?: string | null };
  sales: number;
  purchases: number;
  received: number;
  paid: number;
  journals: number;
  /** Net debit of the vouchers on ledger accounts marked Is Related Party for this party. */
  accounts: number;
  documents: number;
  /** The party's balance on the last day, debit positive: above zero the party owes the company. */
  balance: number | null;
}

export interface RptReport {
  from_date: string;
  to_date: string;
  company: string | null;
  companies: { pan: string; name: string }[];
  relationships: string[];
  register_size: number;
  rows: RptRow[];
}

export const getOpenItems = (period: Period) => call<{ items: OpenItem[]; users: User[] }>("open_items.items", { ...period });
export const saveReview = (item: OpenItem, review: Review) =>
  call<Review>(
    "open_items.review",
    {
      a: item.pair.a.pan,
      b: item.pair.b.pan,
      key: item.key,
      status: review.status,
      assigned_to: review.assigned_to ?? "",
      reason: review.reason ?? "",
      note: review.note ?? "",
    },
    true,
  );

export type SiteState = "Healthy" | "Late" | "Never synced" | "Pending" | "Revoked" | "Disabled";

export interface SiteHealth {
  site_url: string;
  state: SiteState;
  companies: { pan: string; name: string }[];
  fingerprint: string | null;
  approved_on: string | null;
  last_seen: string | null;
  last_sync_on: string | null;
  last_sync_rows: number | null;
  sync_from: string | null;
  last_check: string | null;
  last_result: string | null;
  documents: number;
  latest_document: string | null;
  warnings: string[];
  /** Accepted sync calls in each of the last 24 hours, oldest first. */
  activity: number[];
  rejected: number;
}

export const getSyncHealth = () =>
  call<{ sync_every: number; server_time: string; sites: SiteHealth[] }>("health.sites", {});

export const getInternalTransfers = () =>
  call<{ companies: InternalCompany[]; items: Transfer[] }>("internal_transfers.transfers", {});

export const getRpt =(period: Period, company?: string) => call<RptReport>("rpt.report", { ...period, company });
export const getAssistantStatus =() => call<{ available: boolean; reason: string | null }>("assistant.status", {});
export const ask = (question: string, history: { question: string; answer: string }[]) =>
  call<Answer>("assistant.ask", { question, history: JSON.stringify(history) }, true);

/** Load data whenever `key` changes, dropping the answer to a request that has been superseded. */
export function useLoad<T>(load: () => Promise<T>, key: string) {
  const [state, setState] = useState<{ data?: T; error?: string; loading: boolean }>({ loading: true });
  useEffect(() => {
    let current = true;
    setState((s) => ({ data: s.data, loading: true }));
    load().then(
      (data) => current && setState({ data, loading: false }),
      (e: Error) => current && setState({ error: e.message, loading: false }),
    );
    return () => {
      current = false;
    };
    // `key` stands for everything `load` depends on.
  }, [key]);
  return state;
}

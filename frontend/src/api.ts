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

export interface Line {
  kind: "Invoice" | "Money";
  status: Status;
  date: string;
  a: Doc | null;
  b: Doc | null;
  difference: number;
}

export interface Summary {
  invoice: Counts;
  money: Counts;
  open: number;
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

export interface PairResult {
  a: Company;
  b: Company;
  summary: Summary;
  balance: Balance;
  trend: Trend;
  lines: Line[];
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

export interface Answer {
  ok: boolean;
  answer: string;
  tool?: string;
  lines?: FoundLine[];
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
export const getAssistantStatus = () => call<{ available: boolean; reason: string | null }>("assistant.status", {});
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

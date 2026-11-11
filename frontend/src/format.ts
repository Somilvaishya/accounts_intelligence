import type { Company, Doc, Line, Status } from "./api";

const rupees = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const money = (value: number | null | undefined) => (value == null ? "—" : `₹${rupees.format(value)}`);

const whole = new Intl.NumberFormat("en-IN", { maximumFractionDigits: 0 });

/** ₹7.76 Cr, ₹43.5 L, ₹14,040: for cards and charts. The full figure goes in a tooltip. */
export function moneyShort(value: number | null | undefined) {
  if (value == null) return "—";
  const sign = value < 0 ? "−" : "";
  const abs = Math.abs(value);
  if (abs >= 1e7) return `${sign}₹${(abs / 1e7).toFixed(2)} Cr`;
  if (abs >= 1e5) return `${sign}₹${(abs / 1e5).toFixed(1)} L`;
  return `${sign}₹${whole.format(abs)}`;
}

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

/** 2026-08-14 → 14 Aug 2026 */
export function date(value: string | null | undefined) {
  if (!value) return "—";
  const [y, m, d] = value.split("-");
  return `${Number(d)} ${MONTHS[Number(m) - 1]} ${y}`;
}

/** 2026-08 → Aug */
export const monthName = (value: string) => MONTHS[Number(value.slice(5, 7)) - 1];

/** "4 min ago" from a server datetime (the server's time zone is assumed to be the browser's). */
export function ago(value: string | null | undefined) {
  if (!value) return "never";
  const minutes = Math.round((Date.now() - new Date(value.replace(" ", "T")).getTime()) / 60000);
  if (minutes < 1) return "just now";
  if (minutes < 60) return `${minutes} min ago`;
  if (minutes < 48 * 60) return `${Math.round(minutes / 60)} h ago`;
  return `${Math.round(minutes / 1440)} days ago`;
}

/** "Garg Global Innovative Limited" → "GGIL", "K.G. Overseas Private Limited" → "KGOPL". */
export function acronym(name: string) {
  return name
    .split(/\s+/)
    .map((word) => word.replace(/[^A-Za-z]/g, "").slice(0, word.includes(".") ? undefined : 1))
    .join("")
    .toUpperCase();
}

export function statusLabel(status: Status, a: Company, b: Company) {
  return {
    matched: "Matched",
    amount_differs: "Amount differs",
    date_differs: "Date differs",
    only_a: `Only at ${short(a.name)}`,
    only_b: `Only at ${short(b.name)}`,
  }[status];
}

// Green matched, amber differs, red missing: colour carries meaning only (DESIGN.md).
const GOOD = "bg-emerald-50 text-emerald-800 ring-emerald-200 dark:bg-emerald-500/10 dark:text-emerald-300 dark:ring-emerald-500/30";
const WARN = "bg-amber-50 text-amber-800 ring-amber-200 dark:bg-amber-500/10 dark:text-amber-300 dark:ring-amber-500/30";
const BAD = "bg-rose-50 text-rose-800 ring-rose-200 dark:bg-rose-500/10 dark:text-rose-300 dark:ring-rose-500/30";

export const STATUS_STYLE: Record<Status, string> = {
  matched: GOOD,
  amount_differs: WARN,
  date_differs: WARN,
  only_a: BAD,
  only_b: BAD,
};

export const TONE = { good: GOOD, warn: WARN, bad: BAD };

/** How serious a balance gap is: under ₹1 is rounding, under ₹1 lakh needs a look, more is a problem. */
export function gapTone(gap: number, open: number): keyof typeof TONE {
  const abs = Math.abs(gap);
  if (abs <= 1 && open === 0) return "good";
  return abs >= 1e5 ? "bad" : "warn";
}

export const TONE_TEXT = {
  good: "text-emerald-700 dark:text-emerald-400",
  warn: "text-amber-700 dark:text-amber-400",
  bad: "text-rose-700 dark:text-rose-400",
};

/** "Garg Global Spices Private Limited" → "Garg Global Spices". */
export function short(name: string) {
  return name.replace(/\s+(private\s+limited|pvt\.?\s*ltd\.?|limited|ltd\.?|llp)\s*$/i, "");
}

/** Who owes whom, from one company's balance with the other (debit positive). */
export function owes(balance: number | null, holder: Company, other: Company) {
  if (balance == null) return "No balance received";
  if (Math.abs(balance) < 0.005) return "Settled";
  return balance > 0
    ? `${short(other.name)} owes ${short(holder.name)}`
    : `${short(holder.name)} owes ${short(other.name)}`;
}

/** The document on the company site that holds it. */
export function docUrl(doc: Doc, company: Company) {
  if (!company.site_url) return undefined;
  const slug = doc.doctype.toLowerCase().replace(/ /g, "-");
  return `${company.site_url.replace(/\/$/, "")}/app/${slug}/${encodeURIComponent(doc.name)}`;
}

/** The lines as CSV rows, header first. */
export function csvRows(lines: Line[], a: Company, b: Company) {
  const side = (doc: Doc | null) => [
    doc?.doctype ?? "",
    doc?.name ?? "",
    doc?.date ?? "",
    doc?.amount ?? "",
    doc?.bill_no ?? doc?.reference_no ?? "",
  ];
  const header = ["Date", "Type", "Status"];
  for (const company of [a, b]) {
    const name = short(company.name);
    header.push(`${name} document type`, `${name} document`, `${name} date`, `${name} amount`, `${name} bill / reference`);
  }
  header.push("Difference");
  const rows = lines.map((l) => [l.date, l.kind, statusLabel(l.status, a, b), ...side(l.a), ...side(l.b), l.difference]);
  return [header, ...rows];
}

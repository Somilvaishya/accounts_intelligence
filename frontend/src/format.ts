import type { Company, Doc, InternalSummary, Line, Status } from "./api";

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

// The state a GSTIN is registered in, from its first two digits.
const STATES: Record<string, string> = {
  "01": "J&K", "02": "Himachal", "03": "Punjab", "04": "Chandigarh", "05": "Uttarakhand", "06": "Haryana",
  "07": "Delhi", "08": "Rajasthan", "09": "UP", "10": "Bihar", "11": "Sikkim", "12": "Arunachal",
  "13": "Nagaland", "14": "Manipur", "15": "Mizoram", "16": "Tripura", "17": "Meghalaya", "18": "Assam",
  "19": "West Bengal", "20": "Jharkhand", "21": "Odisha", "22": "Chhattisgarh", "23": "MP", "24": "Gujarat",
  "26": "DNH & DD", "27": "Maharashtra", "29": "Karnataka", "30": "Goa", "31": "Lakshadweep", "32": "Kerala",
  "33": "Tamil Nadu", "34": "Puducherry", "35": "Andaman", "36": "Telangana", "37": "Andhra", "38": "Ladakh",
};

/** Internal transfers of one company that need a look. */
export const transfersToLook = (s: InternalSummary) =>
  s.not_received.count + s.amount_differs.count + s.check_party.count + s.not_dispatched.count;

/** "09AANCR4593B1Z2" → "UP"; the GSTIN itself when the state is unknown, a dash when there is none. */
export const branch = (gstin: string | null | undefined) => (gstin ? STATES[gstin.slice(0, 2)] ?? gstin : "—");

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

/** The document type to show and link to. An account entry is a voucher on a related-party account; its
 * reference holds the voucher's own type. */
export function docType(doc: Doc) {
  return doc.doctype === "Account Entry" && doc.reference_no ? doc.reference_no : doc.doctype;
}

/** The document on the company site that holds it. */
export function docUrl(doc: Doc, company: Company) {
  if (!company.site_url) return undefined;
  const slug = docType(doc).toLowerCase().replace(/ /g, "-");
  return `${company.site_url.replace(/\/$/, "")}/app/${slug}/${encodeURIComponent(doc.name)}`;
}

/** A side's documents: all of a manual match's, otherwise the one the line has. */
export function lineDocs(line: Line, side: "a" | "b"): Doc[] {
  const many = side === "a" ? line.a_docs : line.b_docs;
  const one = side === "a" ? line.a : line.b;
  return many ?? (one ? [one] : []);
}

/** The lines as CSV rows, header first. A manual match's documents share one cell each, joined by " + ". */
export function csvRows(lines: Line[], a: Company, b: Company) {
  const join = (docs: Doc[], value: (doc: Doc) => string | number | undefined) =>
    docs.map((doc) => value(doc) ?? "").join(" + ");
  const side = (docs: Doc[]) => [
    join(docs, (doc) => doc.doctype),
    join(docs, (doc) => doc.name),
    join(docs, (doc) => doc.date),
    docs.length ? Math.round(docs.reduce((sum, doc) => sum + doc.amount, 0) * 100) / 100 : "",
    join(docs, (doc) => doc.bill_no ?? doc.reference_no),
  ];
  const header = ["Date", "Type", "Status"];
  for (const company of [a, b]) {
    const name = short(company.name);
    header.push(`${name} document type`, `${name} document`, `${name} date`, `${name} amount`, `${name} bill / reference`);
  }
  header.push("Difference", "Reconciled by hand");
  const rows = lines.map((l) => [
    l.date, l.kind, statusLabel(l.status, a, b), ...side(lineDocs(l, "a")), ...side(lineDocs(l, "b")), l.difference,
    l.match ? [l.match.by_name ?? l.match.by, l.match.on?.slice(0, 10), l.match.note].filter(Boolean).join(" · ") : "",
  ]);
  return [header, ...rows];
}

import type { Company, Doc, Line, Status } from "./api";

const rupees = new Intl.NumberFormat("en-IN", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

export const money = (value: number | null | undefined) => (value == null ? "—" : `₹${rupees.format(value)}`);

export function date(value: string | null | undefined) {
  if (!value) return "—";
  const [y, m, d] = value.split("-");
  return `${d}-${m}-${y}`;
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

export const STATUS_STYLE: Record<Status, string> = {
  matched: "bg-emerald-50 text-emerald-700 ring-emerald-200",
  amount_differs: "bg-amber-50 text-amber-800 ring-amber-200",
  date_differs: "bg-sky-50 text-sky-700 ring-sky-200",
  only_a: "bg-rose-50 text-rose-700 ring-rose-200",
  only_b: "bg-rose-50 text-rose-700 ring-rose-200",
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

export function toCsv(lines: Line[], a: Company, b: Company) {
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
  const cell = (value: string | number) => {
    const text = String(value);
    return /[",\n]/.test(text) ? `"${text.replace(/"/g, '""')}"` : text;
  };
  return [header, ...rows].map((row) => row.map(cell).join(",")).join("\n");
}

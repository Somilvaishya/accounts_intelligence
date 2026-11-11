import { useEffect, useMemo, useState, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { REASONS, getOpenItems, saveReview, useLoad, type Doc, type OpenItem, type Review, type User } from "../api";
import { AgeingBars } from "../components/Charts";
import Icon from "../components/Icon";
import { STATUS_STYLE, TONE, acronym, date, docUrl, money, moneyShort, statusLabel } from "../format";
import { Message, PeriodFilter, askAssistant, downloadCsv, useShell } from "../Layout";

type Show = "todo" | "In progress" | "Explained" | "all";
const AGES = [
  ["", "Any age"],
  ["30", "Up to 30 days"],
  ["90", "31-90 days"],
  ["180", "91-180 days"],
  ["181", "Over 180 days"],
] as const;
const ME = "__me__";
const NOBODY = "__nobody__";
const USER_ID = document.getElementById("root")?.dataset.user || "";

const reviewOf = (item: OpenItem): Review => item.review ?? { status: "Open", assigned_to: null, reason: null, note: null };

function inAge(age: number, bucket: string) {
  if (!bucket) return true;
  if (bucket === "30") return age <= 30;
  if (bucket === "90") return age > 30 && age <= 90;
  if (bucket === "180") return age > 90 && age <= 180;
  return age > 180;
}

export default function OpenItems() {
  const { period, setPeriod, query } = useShell();
  const loaded = useLoad(() => getOpenItems(period), query);
  const [items, setItems] = useState<OpenItem[]>([]);
  const [show, setShow] = useState<Show>("todo");
  const [pair, setPair] = useState("");
  const [kind, setKind] = useState("");
  const [age, setAge] = useState("");
  const [assignee, setAssignee] = useState("");
  const [search, setSearch] = useState("");

  useEffect(() => setItems(loaded.data?.items ?? []), [loaded.data]);
  const users = loaded.data?.users ?? [];
  const pairs = useMemo(() => {
    const seen = new Map<string, string>();
    for (const i of items) seen.set(`${i.pair.a.pan}/${i.pair.b.pan}`, `${acronym(i.pair.a.name)} ↔ ${acronym(i.pair.b.name)}`);
    return [...seen];
  }, [items]);

  const shown = useMemo(() => {
    const text = search.trim().toLowerCase();
    return items.filter((i) => {
      const r = reviewOf(i);
      if (show === "todo" && r.status === "Explained") return false;
      if (show !== "todo" && show !== "all" && r.status !== show) return false;
      if (pair && `${i.pair.a.pan}/${i.pair.b.pan}` !== pair) return false;
      if (kind && i.kind !== kind) return false;
      if (!inAge(i.age, age)) return false;
      if (assignee === NOBODY && r.assigned_to) return false;
      if (assignee === ME && r.assigned_to !== USER_ID) return false;
      if (assignee && assignee !== NOBODY && assignee !== ME && r.assigned_to !== assignee) return false;
      if (!text) return true;
      return [i.a, i.b].some((d) => [d?.name, d?.bill_no, d?.reference_no, d?.party].some((v) => v?.toLowerCase().includes(text)));
    });
  }, [items, show, pair, kind, age, assignee, search]);

  const update = (item: OpenItem, review: Review) =>
    setItems((list) => list.map((i) => (i.key === item.key ? { ...i, review } : i)));

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-end justify-between gap-3">
        <div>
          <h1 className="text-2xl font-semibold tracking-tight">Open items</h1>
          <p className="mt-0.5 text-sm text-ink-3">Lines the two books disagree on: look into them, assign them, explain them</p>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <PeriodFilter period={period} onChange={setPeriod} />
          <button className="btn-primary" disabled={!shown.length} onClick={() => download(shown)}>
            <Icon name="download" /> CSV
          </button>
        </div>
      </div>

      {loaded.error && <Message tone="error">{loaded.error}</Message>}
      {!loaded.error && loaded.loading && !loaded.data && <Message>Loading…</Message>}
      {loaded.data && items.length === 0 && <Message>Nothing open: every line between the group companies matches.</Message>}
      {loaded.data && items.length > 0 && (
        <>
          <Summary items={items} />
          <div className="grid gap-5 lg:grid-cols-[230px_minmax(0,1fr)]">
            <aside className="card h-max space-y-4 p-4 lg:sticky lg:top-20">
              <Filter label="Status">
                {([["todo", "Needs a look"], ["In progress", "In progress"], ["Explained", "Explained"], ["all", "All"]] as const).map(([value, label]) => (
                  <label key={value} className="flex items-center gap-2 text-sm">
                    <input type="radio" name="show" checked={show === value} onChange={() => setShow(value)} className="accent-[rgb(var(--accent))]" />
                    {label}
                  </label>
                ))}
              </Filter>
              <Filter label="Company pair">
                <select className="field w-full" value={pair} onChange={(e) => setPair(e.target.value)}>
                  <option value="">All pairs</option>
                  {pairs.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Filter>
              <Filter label="Type">
                <select className="field w-full" value={kind} onChange={(e) => setKind(e.target.value)}>
                  <option value="">Invoices and money</option>
                  <option value="Invoice">Invoices</option>
                  <option value="Money">Payments and journals</option>
                </select>
              </Filter>
              <Filter label="Age">
                <select className="field w-full" value={age} onChange={(e) => setAge(e.target.value)}>
                  {AGES.map(([value, label]) => <option key={value} value={value}>{label}</option>)}
                </select>
              </Filter>
              <Filter label="Assigned to">
                <select className="field w-full" value={assignee} onChange={(e) => setAssignee(e.target.value)}>
                  <option value="">Anyone</option>
                  {USER_ID && <option value={ME}>Me</option>}
                  <option value={NOBODY}>Nobody</option>
                  {users.map((u) => <option key={u.name} value={u.name}>{u.full_name || u.name}</option>)}
                </select>
              </Filter>
              <Filter label="Search">
                <input className="field w-full" placeholder="Document, bill, reference" value={search} onChange={(e) => setSearch(e.target.value)} />
              </Filter>
            </aside>

            <div className={`min-w-0 space-y-3 transition-opacity ${loaded.loading ? "opacity-60" : ""}`}>
              <div className="text-sm text-ink-3">
                {shown.length} of {items.length} · {money(shown.reduce((t, i) => t + Math.abs(i.difference), 0))}
              </div>
              {shown.length === 0 && <Message>Nothing matches these filters.</Message>}
              {shown.map((item) => <ItemCard key={item.key} item={item} users={users} query={query} onSaved={update} />)}
            </div>
          </div>
        </>
      )}
    </div>
  );
}

function Filter({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="space-y-1.5">
      <div className="caps">{label}</div>
      {children}
    </div>
  );
}

function Summary({ items }: { items: OpenItem[] }) {
  const todo = items.filter((i) => reviewOf(i).status !== "Explained");
  const ageing = ["0-30 days", "31-90 days", "91-180 days", "Over 180 days"].map((bucket, n) => {
    const inBucket = todo.filter((i) => inAge(i.age, AGES[n + 1][0]));
    return { bucket, count: inBucket.length, amount: inBucket.reduce((t, i) => t + Math.abs(i.difference), 0) };
  });
  const cards: [string, string, string][] = [
    ["Need a look", String(todo.length), `${items.length - todo.length} explained`],
    ["At stake", moneyShort(todo.reduce((t, i) => t + Math.abs(i.difference), 0)), "difference on those lines"],
    ["Oldest", todo.length ? `${Math.max(...todo.map((i) => i.age))} days` : "—", "since the document date"],
    ["Assigned", String(todo.filter((i) => reviewOf(i).assigned_to).length), `${todo.filter((i) => reviewOf(i).status === "In progress").length} in progress`],
  ];
  return (
    <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
      <div className="grid grid-cols-2 gap-4">
        {cards.map(([label, value, caption]) => (
          <div key={label} className="card p-4">
            <span className="caps">{label}</span>
            <div className="mt-1.5 text-2xl font-semibold tracking-tight tabular-nums">{value}</div>
            <div className="mt-0.5 text-xs text-ink-3">{caption}</div>
          </div>
        ))}
      </div>
      <section className="card p-4">
        <h2 className="font-semibold tracking-tight">Age of what needs a look</h2>
        <p className="mb-3 text-xs text-ink-3">Explained lines are left out</p>
        <AgeingBars ageing={ageing} />
      </section>
    </div>
  );
}

function ItemCard({ item, users, query, onSaved }: { item: OpenItem; users: User[]; query: string; onSaved: (i: OpenItem, r: Review) => void }) {
  const review = reviewOf(item);
  const [explaining, setExplaining] = useState(false);
  const [reason, setReason] = useState(review.reason ?? "");
  const [note, setNote] = useState(review.note ?? "");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const { a, b } = item.pair;
  const ageTone = item.age > 180 ? TONE.bad : item.age > 90 ? TONE.warn : "bg-sunken text-ink-2 ring-line";

  const save = async (next: Partial<Review>) => {
    setSaving(true);
    setError("");
    try {
      onSaved(item, await saveReview(item, { ...review, ...next }));
      setExplaining(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSaving(false);
    }
  };

  const assignedName = users.find((u) => u.name === review.assigned_to)?.full_name || review.assigned_to;
  const question = `Explain the open line ${[item.a?.name, item.b?.name].filter(Boolean).join(" / ")} between ${acronym(a.name)} and ${acronym(b.name)}: what is it and what should be done?`;

  return (
    <article className={`card p-4 ${saving ? "opacity-70" : ""}`}>
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="flex flex-wrap items-center gap-2">
          <span className={`rounded-md px-2 py-0.5 text-xs font-semibold tabular-nums ring-1 ring-inset ${ageTone}`}>{item.age} days</span>
          <span className={`rounded-md px-2 py-0.5 text-xs font-medium ring-1 ring-inset ${STATUS_STYLE[item.status]}`}>{statusLabel(item.status, a, b)}</span>
          <Link to={`/pair/${a.pan}/${b.pan}${query}`} className="text-xs text-ink-3 hover:text-accent">
            {acronym(a.name)} ↔ {acronym(b.name)} · {item.kind === "Invoice" ? "Invoice" : "Payment / journal"} · {date(item.date)}
          </Link>
        </div>
        <div className="text-right">
          <div className="text-lg font-semibold tabular-nums">{money(Math.abs(item.difference))}</div>
        </div>
      </div>

      <div className="mt-3 grid gap-3 sm:grid-cols-2">
        <Side doc={item.a} company={a} />
        <Side doc={item.b} company={b} />
      </div>

      {(review.status !== "Open" || review.assigned_to) && (
        <div className="mt-3 flex flex-wrap items-center gap-2 text-xs">
          {review.status !== "Open" && (
            <span className={`rounded-md px-2 py-0.5 font-medium ring-1 ring-inset ${review.status === "Explained" ? TONE.good : TONE.warn}`}>
              {review.status}{review.reason ? `: ${review.reason}` : ""}
            </span>
          )}
          {review.assigned_to && <span className="text-ink-2">Assigned to {assignedName}</span>}
          {review.note && <span className="text-ink-3">“{review.note}”</span>}
        </div>
      )}

      {explaining ? (
        <div className="mt-3 space-y-2 rounded-lg bg-sunken/60 p-3">
          <select className="field w-full" value={reason} onChange={(e) => setReason(e.target.value)}>
            <option value="">Choose a reason…</option>
            {REASONS.map((r) => <option key={r} value={r}>{r}</option>)}
          </select>
          <textarea className="field h-16 w-full py-1.5" placeholder="Note (optional): what was found, what happens next" value={note}
            onChange={(e) => setNote(e.target.value)} maxLength={2000} />
          <div className="flex gap-2">
            <button className="btn-primary" disabled={!reason || saving} onClick={() => save({ status: "Explained", reason, note })}>Save</button>
            <button className="btn" onClick={() => setExplaining(false)}>Cancel</button>
          </div>
        </div>
      ) : (
        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-line pt-3">
          <select className="field" value={review.assigned_to ?? ""} disabled={saving} aria-label="Assign to"
            onChange={(e) => save({ assigned_to: e.target.value || null })}>
            <option value="">Unassigned</option>
            {users.map((u) => <option key={u.name} value={u.name}>{u.full_name || u.name}</option>)}
          </select>
          {review.status === "Open" && <button className="btn" disabled={saving} onClick={() => save({ status: "In progress" })}>In progress</button>}
          {review.status === "Explained" ? (
            <button className="btn" disabled={saving} onClick={() => save({ status: "Open", reason: null })}>Reopen</button>
          ) : (
            <button className="btn" disabled={saving} onClick={() => setExplaining(true)}>
              <Icon name="check" /> Explain
            </button>
          )}
          <button className="btn ml-auto" onClick={() => askAssistant(question)}>
            <Icon name="sparkles" className="h-4 w-4 text-accent" /> Ask AI
          </button>
        </div>
      )}
      {error && <p className="mt-2 text-xs text-rose-700 dark:text-rose-400">{error}</p>}
    </article>
  );
}

function Side({ doc, company }: { doc: Doc | null; company: OpenItem["pair"]["a"] }) {
  if (!doc) {
    return (
      <div className="rounded-lg border border-dashed border-rose-300 px-3 py-2 text-xs text-rose-600 dark:border-rose-500/40 dark:text-rose-400">
        Not recorded in {acronym(company.name)}'s books
      </div>
    );
  }
  const url = docUrl(doc, company);
  return (
    <div className="rounded-lg bg-sunken/60 px-3 py-2 text-xs">
      <div className="flex items-baseline justify-between gap-2">
        {url ? (
          <a href={url} target="_blank" rel="noreferrer" className="truncate text-sm font-medium hover:text-accent">{doc.name}</a>
        ) : (
          <span className="truncate text-sm font-medium">{doc.name}</span>
        )}
        <span className="tabular-nums">{money(doc.amount)}</span>
      </div>
      <div className="text-ink-3">
        {acronym(company.name)} · {doc.doctype}{doc.payment_type ? ` · ${doc.payment_type}` : ""} · {date(doc.date)}
      </div>
      {(doc.bill_no || doc.reference_no) && (
        <div className="truncate text-ink-3" title={doc.bill_no ?? doc.reference_no}>{doc.bill_no ? `Bill ${doc.bill_no}` : doc.reference_no}</div>
      )}
    </div>
  );
}

function download(items: OpenItem[]) {
  downloadCsv("open-items.csv", [
    ["Pair", "Type", "Date", "Age (days)", "Status", "Document A", "Document B", "Difference", "Review", "Assigned to", "Reason", "Note"],
    ...items.map((i) => {
      const r = reviewOf(i);
      return [
        `${acronym(i.pair.a.name)} ↔ ${acronym(i.pair.b.name)}`, i.kind, i.date, i.age, statusLabel(i.status, i.pair.a, i.pair.b),
        i.a?.name, i.b?.name, i.difference, r.status, r.assigned_to, r.reason, r.note,
      ];
    }),
  ]);
}

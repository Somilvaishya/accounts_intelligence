// The assistant panel. Answers come from assistant.ask on the server, worked out from the reconciled data;
// each answer shows the lines it used, linked to their documents, so its figures can be checked.
import { Fragment, useEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { ask, getAssistantStatus, useLoad, type FoundLine } from "../api";
import { STATUS_STYLE, acronym, date, docUrl, money } from "../format";
import Icon from "./Icon";

interface Message {
  role: "user" | "assistant";
  text: string;
  ok?: boolean;
  lines?: FoundLine[];
}

const SUGGESTIONS = [
  "Which pair has the biggest gap, and why?",
  "Which payments are recorded on one side only?",
  "Show the oldest open items",
  "Summarise where the group stands",
];
// The conversation is kept in this browser only; the server stores nothing.
const STORE = "ai-chat";
const TURNS = 4;

function load(): Message[] {
  try {
    return JSON.parse(localStorage.getItem(STORE) || "[]");
  } catch {
    return [];
  }
}

function save(messages: Message[]) {
  try {
    localStorage.setItem(STORE, JSON.stringify(messages.slice(-30)));
  } catch {
    // Not remembered; the chat still works.
  }
}

export default function Chat({ onClose, pending, onPendingSent }: {
  onClose?: () => void;
  /** A question another page asked the assistant (see askAssistant), sent as soon as the panel can. */
  pending?: string | null;
  onPendingSent?: () => void;
}) {
  const status = useLoad(getAssistantStatus, "status");
  const [messages, setMessages] = useState<Message[]>(load);
  const [draft, setDraft] = useState("");
  const [busy, setBusy] = useState(false);
  const end = useRef<HTMLDivElement>(null);
  const available = status.data?.available ?? false;

  useEffect(() => {
    save(messages);
    end.current?.scrollIntoView({ block: "end" });
  }, [messages, busy]);

  const send = async (question: string) => {
    question = question.trim();
    if (!question || busy || !available) return;
    // Earlier answered turns, for follow-up questions.
    const history: { question: string; answer: string }[] = [];
    messages.forEach((m, i) => {
      const reply = messages[i + 1];
      if (m.role === "user" && reply?.role === "assistant" && reply.ok) history.push({ question: m.text, answer: reply.text });
    });
    setMessages((list) => [...list, { role: "user", text: question }]);
    setDraft("");
    setBusy(true);
    try {
      const result = await ask(question, history.slice(-TURNS));
      setMessages((list) => [...list, { role: "assistant", text: result.answer, ok: result.ok, lines: result.lines }]);
    } catch (e) {
      setMessages((list) => [...list, { role: "assistant", text: (e as Error).message, ok: false }]);
    } finally {
      setBusy(false);
    }
  };

  useEffect(() => {
    if (pending && available && !busy) {
      onPendingSent?.();
      send(pending);
    }
    // Only a new question or the assistant becoming available should send it.
  }, [pending, available]);

  const onKey = (e: KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      send(draft);
    }
  };

  return (
    <aside className="card flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-white">
            <Icon name="sparkles" className="h-4 w-4" />
          </span>
          <div>
            <div className="text-sm font-semibold leading-tight">Ask about your group books</div>
            <div className="text-[11px] text-ink-3">Answers only from the reconciled data</div>
          </div>
        </div>
        <div className="flex items-center gap-1">
          {messages.length > 0 && (
            <button className="rounded-md px-2 py-1 text-xs text-ink-3 hover:bg-sunken hover:text-ink" onClick={() => setMessages([])}>
              New chat
            </button>
          )}
          {onClose && (
            <button className="rounded-md p-1 text-ink-3 hover:bg-sunken hover:text-ink" onClick={onClose} aria-label="Close assistant">
              <Icon name="close" />
            </button>
          )}
        </div>
      </div>

      <div className="flex-1 space-y-4 overflow-y-auto p-4">
        {status.data && !available && (
          <div className="rounded-xl bg-sunken p-4 text-sm text-ink-2">{status.data.reason}</div>
        )}
        {messages.length === 0 && available && (
          <div>
            <p className="text-sm text-ink-2">
              Ask in plain language. Answers come from the books synced here, with the documents behind them.
            </p>
            <div className="mt-3 flex flex-col items-start gap-1.5">
              {SUGGESTIONS.map((s) => (
                <button key={s} onClick={() => send(s)}
                  className="rounded-full border border-line px-3 py-1 text-left text-xs text-ink-2 hover:border-accent hover:text-accent">
                  {s}
                </button>
              ))}
            </div>
          </div>
        )}
        {messages.map((m, i) =>
          m.role === "user" ? (
            <div key={i} className="ml-8 rounded-2xl rounded-tr-sm bg-sunken px-3 py-2 text-sm">{m.text}</div>
          ) : (
            <div key={i} className="space-y-2">
              <div className={`text-sm leading-relaxed ${m.ok === false ? "text-rose-700 dark:text-rose-400" : ""}`}>
                <Rich text={m.text} />
              </div>
              {!!m.lines?.length && <Found lines={m.lines} />}
            </div>
          ),
        )}
        {busy && (
          <div className="flex items-center gap-2 text-xs text-ink-3">
            <span className="h-2 w-2 animate-pulse rounded-full bg-accent" /> Checking the books…
          </div>
        )}
        <div ref={end} />
      </div>

      <div className="border-t border-line p-3">
        <div className="flex items-end gap-2 rounded-xl border border-line bg-surface p-2 focus-within:border-accent">
          <textarea rows={2} value={draft} onChange={(e) => setDraft(e.target.value)} onKeyDown={onKey} maxLength={1000}
            disabled={!available || busy}
            placeholder={available ? "Ask about balances, open items or a document…" : "The assistant is not available"}
            className="flex-1 resize-none bg-transparent text-sm placeholder:text-ink-3 focus:outline-none disabled:cursor-not-allowed" />
          <button onClick={() => send(draft)} disabled={!available || busy || !draft.trim()} aria-label="Send"
            className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-white disabled:opacity-40">
            <Icon name="arrowUp" className="h-3.5 w-3.5" />
          </button>
        </div>
        <p className="mt-1.5 text-center text-[10px] text-ink-3">Enter to send · Shift+Enter for a new line</p>
      </div>
    </aside>
  );
}

/** Paragraphs, "- " bullets and **bold**: all the formatting the answers use. Built as elements, never HTML. */
function Rich({ text }: { text: string }) {
  const blocks = text.split(/\n{2,}/);
  return (
    <div className="space-y-2">
      {blocks.map((block, i) => {
        const rows = block.split("\n");
        if (rows.every((r) => /^\s*[-•*]\s/.test(r))) {
          return (
            <ul key={i} className="list-disc space-y-0.5 pl-5">
              {rows.map((r, j) => <li key={j}>{bold(r.replace(/^\s*[-•*]\s/, ""))}</li>)}
            </ul>
          );
        }
        return <p key={i}>{rows.map((r, j) => <Fragment key={j}>{j > 0 && <br />}{bold(r)}</Fragment>)}</p>;
      })}
    </div>
  );
}

function bold(text: string): ReactNode[] {
  return text.split(/\*\*(.+?)\*\*/g).map((part, i) => (i % 2 ? <strong key={i}>{part}</strong> : part));
}

/** The lines an answer used, linked to their documents, and to the pair when they all share one. */
function Found({ lines }: { lines: FoundLine[] }) {
  const pairs = new Set(lines.map((l) => `${l.pair.a.pan}/${l.pair.b.pan}`));
  const only = pairs.size === 1 ? lines[0].pair : null;
  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <div className="caps flex items-center justify-between bg-sunken/70 px-2.5 py-1.5">
        <span>{lines.length === 1 ? "1 line" : `${lines.length} lines`}</span>
        {only && (
          <Link to={`/pair/${only.a.pan}/${only.b.pan}`} className="normal-case tracking-normal text-accent hover:underline">
            Open {acronym(only.a.name)} ↔ {acronym(only.b.name)}
          </Link>
        )}
      </div>
      <div className="divide-y divide-line">
        {lines.map((line, i) => {
          const doc = line.a ?? line.b;
          const company = line.a ? line.pair.a : line.pair.b;
          const url = doc ? docUrl(doc, company) : undefined;
          return (
            <div key={i} className="flex items-start justify-between gap-2 px-2.5 py-1.5 text-xs">
              <div className="min-w-0">
                <div className="flex items-center gap-1">
                  {url ? (
                    <a href={url} target="_blank" rel="noreferrer" className="truncate font-medium hover:text-accent">{doc?.name}</a>
                  ) : (
                    <span className="truncate font-medium">{doc?.name}</span>
                  )}
                  {line.a && line.b && <span className="truncate text-ink-3">↔ {line.b.name}</span>}
                </div>
                <div className="text-ink-3">{date(line.date)}</div>
              </div>
              <div className="shrink-0 text-right">
                <div className="font-semibold tabular-nums">{money(Math.abs(line.difference))}</div>
                <span className={`inline-block rounded px-1.5 text-[10px] ring-1 ring-inset ${STATUS_STYLE[line.status]}`}>
                  {line.status_label}
                </span>
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

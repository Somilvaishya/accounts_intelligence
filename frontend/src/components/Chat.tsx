// The assistant panel. The layout is final; answering questions is the next step, so until then the panel
// says so plainly instead of showing made-up answers.
import Icon from "./Icon";

const SUGGESTIONS = [
  "Why do two companies' balances differ?",
  "Which invoices are missing?",
  "Oldest open items",
  "Summarise this month",
];

export default function Chat({ onClose }: { onClose?: () => void }) {
  return (
    <aside className="card flex h-full flex-col">
      <div className="flex items-center justify-between border-b border-line px-4 py-3">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-white">
            <Icon name="sparkles" className="h-4 w-4" />
          </span>
          <div>
            <div className="text-sm font-semibold leading-tight">Ask about your group books</div>
            <div className="text-[11px] text-ink-3">Answers only from data you can access</div>
          </div>
        </div>
        {onClose && (
          <button className="rounded-md p-1 text-ink-3 hover:bg-sunken hover:text-ink" onClick={onClose} aria-label="Close chat">
            <Icon name="close" />
          </button>
        )}
      </div>

      <div className="flex flex-1 flex-col justify-end gap-4 overflow-y-auto p-4">
        <div className="rounded-xl bg-sunken p-4 text-sm leading-relaxed text-ink-2">
          <p className="font-medium text-ink">Coming next</p>
          <p className="mt-1">
            Ask in plain language and get answers with figures and links to the exact documents, drawn only from the
            books synced here. The reconciliation on the left is live today; the assistant is the next step.
          </p>
        </div>
        <div>
          <div className="caps mb-2">You will be able to ask</div>
          <div className="flex flex-wrap gap-1.5">
            {SUGGESTIONS.map((s) => (
              <span key={s} className="rounded-full border border-line px-2.5 py-1 text-xs text-ink-3">{s}</span>
            ))}
          </div>
        </div>
      </div>

      <div className="border-t border-line p-3">
        <div className="flex items-end gap-2 rounded-xl border border-line bg-sunken/60 p-2">
          <textarea rows={2} disabled placeholder="The assistant is not connected yet"
            className="flex-1 resize-none bg-transparent text-sm placeholder:text-ink-3 focus:outline-none disabled:cursor-not-allowed" />
          <button disabled className="flex h-7 w-7 items-center justify-center rounded-lg bg-accent text-white opacity-40" aria-label="Send">
            <Icon name="arrowUp" className="h-3.5 w-3.5" />
          </button>
        </div>
      </div>
    </aside>
  );
}

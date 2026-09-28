function StateIcon({ state }) {
  if (state === "active") {
    return <span role="img" aria-label="In progress" className="inline-block h-3 w-3 rounded-full border-2 border-current border-t-transparent animate-spin motion-reduce:animate-none" />;
  }
  const icons = { completed: "✓", failed: "!", stopped: "■", cancelled: "■", unconfirmed: "?", waiting: "○" };
  return <span aria-label={state}>{icons[state] || "?"}</span>;
}

export default function Activity() {
  const entries = props.entries || [];
  const latest = entries[entries.length - 1];
  const state = latest?.state || props.outcome;
  return (
    <section className="rounded-xl border p-3 space-y-2 text-sm" aria-label="Investigation activity">
      <div className="flex items-center gap-2" role="status" aria-live="polite">
        <StateIcon state={state} />
        <strong className="font-medium break-words">{props.current}</strong>
      </div>
      <p className="text-xs text-muted-foreground">Read-only · {Number(props.elapsed_seconds || 0).toFixed(1)}s elapsed at last event</p>
      <details>
        <summary className="cursor-pointer text-muted-foreground">Activity history ({entries.length})</summary>
        <ol className="mt-3 space-y-2 max-h-64 overflow-y-auto">
          {entries.map((entry) => (
            <li key={entry.id} className="flex gap-2 items-start">
              <span className="shrink-0"><StateIcon state={entry.state} /></span>
              <div className="min-w-0 break-words">
                <span>{entry.title}</span>
                <span className="text-xs text-muted-foreground"> · +{entry.started_seconds.toFixed(1)}s{entry.duration_seconds !== undefined ? ` · ${entry.duration_seconds.toFixed(1)}s duration` : ""}</span>
                {entry.state === "unconfirmed" && <p className="text-xs text-muted-foreground">Completion event not observed.</p>}
              </div>
            </li>
          ))}
        </ol>
      </details>
      {props.trace && (
        <div className="pt-1">
          <button
            type="button"
            onClick={() => {
              if (typeof window !== "undefined" && window.__DEVOPS_OPEN_EXECUTION_VIEWER__) {
                window.__DEVOPS_OPEN_EXECUTION_VIEWER__(props.trace);
              }
            }}
            className="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-lg bg-slate-800/90 hover:bg-slate-700 text-slate-200 hover:text-white border border-slate-700 text-xs font-medium transition-all cursor-pointer"
          >
            <span>📊</span>
            <span>View Execution Details</span>
          </button>
        </div>
      )}
    </section>
  );
}

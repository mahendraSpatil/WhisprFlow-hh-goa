import { Activity, FolderGit2, History, Loader2, Play, RefreshCcw } from "lucide-react";
import { useEffect, useState, type FormEvent } from "react";

import type { RunState } from "../state/runReducer";
import { isRunActive } from "../state/runReducer";
import { formatDuration } from "../format";

const LAST_SOURCE_KEY = "codeloop:last-source";

function readLastSource(): string {
  try {
    return window.localStorage.getItem(LAST_SOURCE_KEY) ?? "";
  } catch {
    return "";
  }
}

const STATUS_LABELS: Record<RunState["status"], string> = {
  idle: "Idle",
  starting: "Starting",
  queued: "Queued",
  running: "Running",
  completed: "Completed",
  completed_with_escalations: "Escalations",
  failed: "Failed",
};

export type View = "analysis" | "history";

interface TopBarProps {
  run: RunState;
  onRun: (source: string) => void;
  view: View;
  onView: (view: View) => void;
}

export function TopBar({ run, onRun, view, onView }: TopBarProps) {
  const [source, setSource] = useState(readLastSource);
  const active = isRunActive(run.status);

  // Attaching to ?run=<id> fills the input with that run's source.
  useEffect(() => {
    if (run.source) setSource((current) => current || run.source);
  }, [run.source]);

  function submit(event: FormEvent) {
    event.preventDefault();
    const value = source.trim();
    if (!value || active) return;
    try {
      window.localStorage.setItem(LAST_SOURCE_KEY, value);
    } catch {
      // storage unavailable; remembering the last source is only a convenience
    }
    onRun(value);
  }

  return (
    <header className="topbar">
      <div className="brand">
        <span className="brand__mark" aria-hidden>
          <RefreshCcw size={15} strokeWidth={2.6} />
        </span>
        <span className="brand__name">CodeLoop</span>
      </div>

      <nav className="tabs" aria-label="Views">
        <button className={view === "analysis" ? "tab is-active" : "tab"} onClick={() => onView("analysis")} aria-current={view === "analysis" ? "page" : undefined}>
          <Activity size={14} /> Analysis
        </button>
        <button className={view === "history" ? "tab is-active" : "tab"} onClick={() => onView("history")} aria-current={view === "history" ? "page" : undefined}>
          <History size={14} /> History
        </button>
      </nav>

      <form className="run-form" onSubmit={submit}>
        <label className="run-form__field">
          <FolderGit2 size={16} className="run-form__icon" aria-hidden />
          <input
            className="run-form__input"
            value={source}
            onChange={(e) => setSource(e.target.value)}
            placeholder="https://github.com/org/repo.git  or  /path/to/local/repo"
            spellCheck={false}
            autoComplete="off"
            aria-label="Repository URL or local path"
          />
        </label>
        <button className="btn btn--primary" type="submit" disabled={active || !source.trim()}>
          {active ? <Loader2 size={15} className="spin" /> : <Play size={15} fill="currentColor" />}
          {active ? "Running" : "Run"}
        </button>
      </form>

      <RunBadge run={run} />
    </header>
  );
}

function RunBadge({ run }: { run: RunState }) {
  const active = isRunActive(run.status);
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setNow(Date.now()), 250);
    return () => window.clearInterval(timer);
  }, [active]);

  if (run.status === "idle" && !run.runId) {
    return <div className="run-badge run-badge--empty">No run yet</div>;
  }
  const elapsed = run.startedAt ? (run.finishedAt ?? now) - run.startedAt : null;
  return (
    <div className={`run-badge run-badge--${run.status}`} title={run.statusDetail ?? undefined}>
      <span className="run-badge__dot" />
      <span className="run-badge__status">{STATUS_LABELS[run.status]}</span>
      {run.runId && <span className="run-badge__id">{run.runId}</span>}
      {elapsed !== null && <span className="run-badge__time">{formatDuration(elapsed)}</span>}
    </div>
  );
}

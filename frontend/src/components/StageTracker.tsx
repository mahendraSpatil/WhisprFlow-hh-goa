import { AlertOctagon, CheckCircle2, Circle, CircleSlash, Loader2, RotateCcw } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";

import type { StageStatus } from "../api/types";
import { formatDuration } from "../format";
import type { RunState, StageView } from "../state/runReducer";
import { STAGES } from "../stages";
import { RegressionCard } from "./RegressionCard";

const STATUS_TEXT: Record<StageStatus, string> = {
  pending: "Pending",
  running: "Running",
  success: "Done",
  failed: "Retrying",
  escalated: "Escalated",
  skipped: "Skipped",
};

function StatusIcon({ status }: { status: StageStatus }) {
  const size = 18;
  switch (status) {
    case "pending":
      return <Circle size={size} />;
    case "running":
      return <Loader2 size={size} className="spin" />;
    case "success":
      return <CheckCircle2 size={size} />;
    case "failed":
      return <RotateCcw size={size} className="spin-reverse" />;
    case "escalated":
      return <AlertOctagon size={size} />;
    case "skipped":
      return <CircleSlash size={size} />;
  }
}

export function StageTracker({ run }: { run: RunState }) {
  const views = STAGES.map((s) => run.stages[s.name]);
  const done = views.filter((v) => v.status === "success").length;
  const anyRunning = views.some((v) => v.status === "running" || v.status === "failed");
  const [now, setNow] = useState(Date.now());

  useEffect(() => {
    if (!anyRunning) return;
    const timer = window.setInterval(() => setNow(Date.now()), 200);
    return () => window.clearInterval(timer);
  }, [anyRunning]);

  return (
    <aside className="panel stages" aria-label="Pipeline stages">
      <div className="panel__header">
        <span className="panel__title">Pipeline</span>
        <span className="panel__meta">
          {done}/{STAGES.length}
        </span>
      </div>
      <div className="stages__progress" aria-hidden>
        <div className="stages__progress-bar" style={{ width: `${(done / STAGES.length) * 100}%` }} />
      </div>
      <ol className="stages__list">
        {STAGES.map((info, index) => (
          <StageRow
            key={info.name}
            index={index}
            title={info.title}
            description={info.description}
            view={run.stages[info.name]}
            now={now}
            extra={info.name === "regression_check" && run.stages.regression_check.status === "success" ? <RegressionCard run={run} /> : null}
          />
        ))}
      </ol>
    </aside>
  );
}

interface StageRowProps {
  index: number;
  title: string;
  description: string;
  view: StageView;
  now: number;
  /** Rendered under the stage's notes, e.g. the before/after card. */
  extra?: ReactNode;
}

function StageRow({ index, title, description, view, now, extra }: StageRowProps) {
  const { status } = view;
  const duration = view.startedAt ? (view.finishedAt ?? now) - view.startedAt : null;
  const showDuration = duration !== null && status !== "pending" && status !== "skipped";

  return (
    <li className={`stage stage--${status}`}>
      <div className="stage__rail">
        <span className="stage__icon" title={STATUS_TEXT[status]}>
          <StatusIcon status={status} />
        </span>
        <span className="stage__line" />
      </div>
      <div className="stage__body">
        <div className="stage__head">
          <span className="stage__index">{String(index + 1).padStart(2, "0")}</span>
          <span className="stage__title">{title}</span>
          {showDuration && <span className="stage__time">{formatDuration(duration)}</span>}
        </div>
        <div className="stage__description">{description}</div>
        {(status === "failed" || (status === "running" && view.attempt > 1)) && (
          <div className="stage__note stage__note--warn">
            {status === "failed" ? `Attempt ${view.attempt} failed, retrying` : `Attempt ${view.attempt}`}
            {view.error && <span className="stage__note-detail">{view.error.message}</span>}
          </div>
        )}
        {status === "escalated" && view.error && (
          <div className="stage__note stage__note--error" title={view.error.traceback ?? view.error.message}>
            <span className="stage__note-type">{view.error.timed_out ? "Timed out" : view.error.type}</span>
            <span className="stage__note-detail">{view.error.message}</span>
          </div>
        )}
        {status === "skipped" && view.skippedReason && <div className="stage__note">{view.skippedReason}</div>}
        {extra}
      </div>
    </li>
  );
}

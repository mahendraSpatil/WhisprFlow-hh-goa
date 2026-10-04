import { ArrowDownToLine, PanelRightClose, PanelRightOpen, ScrollText } from "lucide-react";
import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { formatClock } from "../format";
import type { LogLine, RunState } from "../state/runReducer";
import { STAGE_TITLES } from "../stages";
import { LogSkeleton } from "./Skeletons";

type Filter = "all" | "warnings";

interface LogPanelProps {
  run: RunState;
  collapsed: boolean;
  onToggle: () => void;
}

export function LogPanel({ run, collapsed, onToggle }: LogPanelProps) {
  const [filter, setFilter] = useState<Filter>("all");
  const [follow, setFollow] = useState(true);
  const [seenCount, setSeenCount] = useState(0);
  const body = useRef<HTMLDivElement>(null);

  const lines = filter === "all" ? run.logs : run.logs.filter((l) => l.level === "warning" || l.level === "error");
  const issues = run.logs.filter((l) => l.level === "warning" || l.level === "error").length;

  // While expanded, everything is "seen"; collapsed, count what arrives.
  useEffect(() => {
    if (!collapsed) setSeenCount(run.logs.length);
  }, [collapsed, run.logs.length]);
  useEffect(() => {
    if (run.logs.length === 0) setSeenCount(0);
  }, [run.logs.length]);

  useLayoutEffect(() => {
    if (follow && body.current) body.current.scrollTop = body.current.scrollHeight;
  }, [lines.length, follow, collapsed]);

  function onScroll() {
    const el = body.current;
    if (!el) return;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
    if (atBottom !== follow) setFollow(atBottom);
  }

  if (collapsed) {
    const unseen = Math.max(0, run.logs.length - seenCount);
    return (
      <aside className="panel logs logs--collapsed">
        <button className="icon-btn" onClick={onToggle} title="Show logs" aria-label="Show logs">
          <PanelRightOpen size={17} />
        </button>
        <div className="logs__rail-label">
          <ScrollText size={14} /> Logs
        </div>
        {unseen > 0 && <span className="logs__badge">{unseen > 99 ? "99+" : unseen}</span>}
      </aside>
    );
  }

  return (
    <aside className="panel logs" aria-label="Run logs">
      <div className="panel__header">
        <span className="panel__title">Logs</span>
        <span className="panel__meta">{run.logs.length}</span>
        <div className="segmented" role="group" aria-label="Log filter">
          <button className={filter === "all" ? "is-active" : ""} onClick={() => setFilter("all")}>
            All
          </button>
          <button className={filter === "warnings" ? "is-active" : ""} onClick={() => setFilter("warnings")}>
            Issues{issues > 0 && <span className="segmented__count">{issues}</span>}
          </button>
        </div>
        <button className="icon-btn" onClick={onToggle} title="Hide logs" aria-label="Hide logs">
          <PanelRightClose size={17} />
        </button>
      </div>
      <div className="logs__body" ref={body} onScroll={onScroll}>
        {lines.length === 0 ? (
          !run.runId ? (
            <div className="logs__empty">Logs from each agent stream here during a run.</div>
          ) : run.logs.length === 0 ? (
            <LogSkeleton />
          ) : (
            <div className="logs__empty">No warnings or errors so far.</div>
          )
        ) : (
          lines.map((line) => <LogRow key={line.seq} line={line} />)
        )}
      </div>
      {!follow && lines.length > 0 && (
        <button
          className="logs__jump"
          onClick={() => {
            setFollow(true);
            if (body.current) body.current.scrollTop = body.current.scrollHeight;
          }}
        >
          <ArrowDownToLine size={14} /> Latest
        </button>
      )}
    </aside>
  );
}

function LogRow({ line }: { line: LogLine }) {
  return (
    <div className={`log log--${line.level}`}>
      <span className="log__time" title={formatClock(line.ts)}>
        {formatClock(line.ts).slice(0, 8)}
      </span>
      <span className="log__message">
        <span className="log__source">{line.stage ? STAGE_TITLES[line.stage] : "run"}</span>
        {line.message}
      </span>
    </div>
  );
}

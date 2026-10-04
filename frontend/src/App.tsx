import { X } from "lucide-react";
import { useState } from "react";

import { GraphCanvas } from "./components/GraphCanvas";
import { HistoryView } from "./components/HistoryView";
import { LogPanel } from "./components/LogPanel";
import { StageTracker } from "./components/StageTracker";
import { TopBar, type View } from "./components/TopBar";
import { useRun } from "./state/useRun";

const LOGS_COLLAPSED_KEY = "codeloop:logs-collapsed";

function readCollapsed(): boolean {
  try {
    return window.localStorage.getItem(LOGS_COLLAPSED_KEY) === "1";
  } catch {
    return false;
  }
}

export default function App() {
  const { state, start, dismissError, selectNode } = useRun();
  const [logsCollapsed, setLogsCollapsed] = useState(readCollapsed);
  const [view, setView] = useState<View>("analysis");

  function toggleLogs() {
    setLogsCollapsed((collapsed) => {
      try {
        window.localStorage.setItem(LOGS_COLLAPSED_KEY, collapsed ? "0" : "1");
      } catch {
        // storage unavailable; the panel state just won't persist
      }
      return !collapsed;
    });
  }

  return (
    <div className={`app ${logsCollapsed ? "app--logs-collapsed" : ""}`}>
      <TopBar run={state} onRun={start} view={view} onView={setView} />
      {state.error && (
        <div className="banner" role="alert">
          <span>{state.error}</span>
          <button className="icon-btn" onClick={dismissError} aria-label="Dismiss">
            <X size={15} />
          </button>
        </div>
      )}
      {/* Both views stay mounted: switching tabs must not drop the graph's viewport or the live run. */}
      <main className={view === "analysis" ? "workspace" : "workspace is-hidden"}>
        <StageTracker run={state} />
        <GraphCanvas run={state} onSelectNode={selectNode} onRun={start} />
        <LogPanel run={state} collapsed={logsCollapsed} onToggle={toggleLogs} />
      </main>
      <div className={view === "history" ? "history-pane" : "history-pane is-hidden"}>
        <HistoryView refreshKey={state.finishedAt ?? 0} active={view === "history"} />
      </div>
    </div>
  );
}

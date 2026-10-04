import type {
  Diagnostics,
  Graph,
  LogLevel,
  Patch,
  RegressionCheckOutput,
  RunEvent,
  RunStatus,
  StageError,
  StageName,
  StageStatus,
} from "../api/types";
import { STAGES, STAGE_TITLES } from "../stages";

export const MAX_LOG_LINES = 5000;

export interface StageView {
  name: StageName;
  status: StageStatus;
  attempt: number;
  error: StageError | null;
  skippedReason: string | null;
  startedAt: number | null;
  finishedAt: number | null;
}

export interface LogLine {
  seq: number;
  ts: number;
  stage: StageName | null;
  /** "status" marks lines synthesized from run/stage status changes. */
  level: LogLevel | "status";
  message: string;
}

export type GraphState = "none" | "loading" | "ready" | "unavailable";

export interface RunState {
  runId: string | null;
  source: string;
  /** "idle" before any run; "starting" while POST /runs is in flight. */
  status: RunStatus | "idle" | "starting";
  statusDetail: string | null;
  startedAt: number | null;
  finishedAt: number | null;
  stages: Record<StageName, StageView>;
  logs: LogLine[];
  lastSeq: number;
  graph: Graph | null;
  graphState: GraphState;
  graphMessage: string | null;
  /** Findings and root causes; reloaded as DiagnosticSentinel and RootCauseDiagnostician finish. */
  diagnostics: Diagnostics | null;
  /** One patch per root cause, once PatchMaster has finished. */
  patches: Patch[] | null;
  /** Verdicts and the before/after summary, once RegressionCheck has finished. */
  regression: RegressionCheckOutput | null;
  /** The node whose findings are open in the drawer. */
  selectedNodeId: string | null;
  error: string | null;
}

export type RunAction =
  | { type: "start"; source: string }
  | { type: "attach"; runId: string; source: string; status: RunStatus }
  | { type: "created"; runId: string; source: string }
  | { type: "event"; event: RunEvent }
  | { type: "graphLoading"; runId: string }
  | { type: "graphLoaded"; runId: string; graph: Graph }
  | { type: "graphUnavailable"; runId: string; message: string }
  | { type: "diagnosticsLoaded"; runId: string; diagnostics: Diagnostics }
  | { type: "patchesLoaded"; runId: string; patches: Patch[] }
  | { type: "regressionLoaded"; runId: string; regression: RegressionCheckOutput }
  | { type: "selectNode"; nodeId: string | null }
  | { type: "error"; message: string }
  | { type: "dismissError" };

function freshStages(): Record<StageName, StageView> {
  return Object.fromEntries(
    STAGES.map((s) => [
      s.name,
      { name: s.name, status: "pending", attempt: 0, error: null, skippedReason: null, startedAt: null, finishedAt: null },
    ]),
  ) as Record<StageName, StageView>;
}

export const initialRunState: RunState = {
  runId: null,
  source: "",
  status: "idle",
  statusDetail: null,
  startedAt: null,
  finishedAt: null,
  stages: freshStages(),
  logs: [],
  lastSeq: 0,
  graph: null,
  graphState: "none",
  graphMessage: null,
  diagnostics: null,
  patches: null,
  regression: null,
  selectedNodeId: null,
  error: null,
};

const TERMINAL_STAGE: ReadonlySet<StageStatus> = new Set(["success", "escalated", "skipped"]);
const TERMINAL_RUN: ReadonlySet<string> = new Set(["completed", "completed_with_escalations", "failed"]);

const RUN_STATUS_TEXT: Record<RunStatus, string> = {
  queued: "Run queued",
  running: "Run started",
  completed: "Run completed",
  completed_with_escalations: "Run completed with escalations",
  failed: "Run failed",
};

function appendLog(logs: LogLine[], line: LogLine): LogLine[] {
  const next = logs.length >= MAX_LOG_LINES ? logs.slice(logs.length - MAX_LOG_LINES + 1) : logs.slice();
  next.push(line);
  return next;
}

function applyEvent(state: RunState, event: RunEvent): RunState {
  if (event.seq <= state.lastSeq || (state.runId && event.run_id !== state.runId)) return state;
  const ts = Date.parse(event.ts);
  const base = { ...state, lastSeq: event.seq };

  switch (event.type) {
    case "log":
      return {
        ...base,
        logs: appendLog(state.logs, { seq: event.seq, ts, stage: event.stage, level: event.level, message: event.message }),
      };

    case "run.status": {
      const terminal = TERMINAL_RUN.has(event.status);
      const message = RUN_STATUS_TEXT[event.status] + (event.detail ? `: ${event.detail}` : "");
      return {
        ...base,
        status: event.status,
        statusDetail: event.detail,
        startedAt: event.status === "running" ? ts : state.startedAt,
        finishedAt: terminal ? ts : state.finishedAt,
        logs:
          event.status === "queued"
            ? state.logs
            : appendLog(state.logs, { seq: event.seq, ts, stage: null, level: "status", message }),
      };
    }

    case "stage.status": {
      const prev = state.stages[event.stage];
      if (!prev) return base;
      const stage: StageView = {
        ...prev,
        status: event.status,
        attempt: event.attempt,
        error: event.error ?? (event.status === "running" ? prev.error : null),
        skippedReason: event.skipped_reason,
        startedAt: event.status === "running" && prev.startedAt === null ? ts : prev.startedAt,
        finishedAt: TERMINAL_STAGE.has(event.status) ? ts : null,
      };
      let logs = state.logs;
      if (event.status !== "pending") {
        const title = STAGE_TITLES[event.stage];
        const detail =
          event.status === "running" && event.attempt > 1
            ? ` (attempt ${event.attempt})`
            : event.error
              ? `: ${event.error.type}: ${event.error.message}`
              : event.skipped_reason
                ? `: ${event.skipped_reason}`
                : "";
        logs = appendLog(logs, { seq: event.seq, ts, stage: event.stage, level: "status", message: `${title} ${event.status}${detail}` });
      }
      return { ...base, stages: { ...state.stages, [event.stage]: stage }, logs };
    }
  }
}

export function runReducer(state: RunState, action: RunAction): RunState {
  switch (action.type) {
    case "start":
      return { ...initialRunState, stages: freshStages(), source: action.source, status: "starting" };
    case "attach":
      return { ...initialRunState, stages: freshStages(), runId: action.runId, source: action.source, status: action.status };
    case "created":
      return { ...state, runId: action.runId, source: action.source, status: "queued" };
    case "event":
      return applyEvent(state, action.event);
    case "graphLoading":
      return action.runId === state.runId ? { ...state, graphState: "loading", graphMessage: null } : state;
    case "graphLoaded":
      return action.runId === state.runId ? { ...state, graph: action.graph, graphState: "ready", graphMessage: null } : state;
    case "graphUnavailable":
      return action.runId === state.runId ? { ...state, graphState: "unavailable", graphMessage: action.message } : state;
    case "diagnosticsLoaded":
      return action.runId === state.runId ? { ...state, diagnostics: action.diagnostics } : state;
    case "patchesLoaded":
      return action.runId === state.runId ? { ...state, patches: action.patches } : state;
    case "regressionLoaded":
      return action.runId === state.runId ? { ...state, regression: action.regression } : state;
    case "selectNode":
      return state.selectedNodeId === action.nodeId ? state : { ...state, selectedNodeId: action.nodeId };
    case "error":
      return { ...state, error: action.message, status: state.status === "starting" ? "idle" : state.status };
    case "dismissError":
      return { ...state, error: null };
  }
}

export function isRunActive(status: RunState["status"]): boolean {
  return status === "starting" || status === "queued" || status === "running";
}

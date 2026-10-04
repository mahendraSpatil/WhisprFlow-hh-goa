import { useCallback, useEffect, useReducer, useRef } from "react";

import {
  ApiError,
  createRun,
  eventsUrl,
  getDiagnostics,
  getGraph,
  getPatches,
  getRegression,
  getRun,
} from "../api/client";
import { TERMINAL_RUN_STATUSES, type RunEvent } from "../api/types";
import { initialRunState, runReducer } from "./runReducer";

const WS_CLOSE_UNKNOWN_RUN = 4404;
const MAX_RECONNECT_DELAY_MS = 5000;

function setRunInUrl(runId: string | null) {
  const url = new URL(window.location.href);
  if (runId) url.searchParams.set("run", runId);
  else url.searchParams.delete("run");
  window.history.replaceState(null, "", url);
}

/**
 * Owns one run at a time: starts it, follows its event stream (reconnecting
 * with ``since`` so nothing is missed or duplicated), and loads the graph as
 * soon as PipelineArchitect succeeds.
 */
export function useRun() {
  const [state, dispatch] = useReducer(runReducer, initialRunState);
  const socket = useRef<WebSocket | null>(null);
  const runId = useRef<string | null>(null);
  const lastSeq = useRef(0);
  const finished = useRef(false);
  const reconnectTimer = useRef<number | undefined>(undefined);
  const reconnectAttempts = useRef(0);

  // The graph and diagnostics are refetched as stages finish. Only the latest response may apply,
  // or a slow earlier one (graph without finding status, diagnostics without root causes) could win.
  const graphTicket = useRef(0);
  const diagnosticsTicket = useRef(0);
  const patchesTicket = useRef(0);
  const regressionTicket = useRef(0);

  const loadGraph = useCallback(async (id: string) => {
    const ticket = ++graphTicket.current;
    dispatch({ type: "graphLoading", runId: id });
    try {
      const graph = await getGraph(id);
      if (ticket === graphTicket.current) dispatch({ type: "graphLoaded", runId: id, graph });
    } catch (err) {
      if (ticket === graphTicket.current) {
        dispatch({ type: "graphUnavailable", runId: id, message: err instanceof Error ? err.message : String(err) });
      }
    }
  }, []);

  // Diagnostics are optional: a run whose scanners all failed simply has none to show.
  const loadDiagnostics = useCallback(async (id: string) => {
    const ticket = ++diagnosticsTicket.current;
    try {
      const diagnostics = await getDiagnostics(id);
      if (ticket === diagnosticsTicket.current) dispatch({ type: "diagnosticsLoaded", runId: id, diagnostics });
    } catch {
      // 409 until DiagnosticSentinel succeeds; the stage tracker already shows why
    }
  }, []);

  const loadPatches = useCallback(async (id: string) => {
    const ticket = ++patchesTicket.current;
    try {
      const patches = await getPatches(id);
      if (ticket === patchesTicket.current) dispatch({ type: "patchesLoaded", runId: id, patches });
    } catch {
      // 409 until PatchMaster succeeds; the stage tracker already shows why
    }
  }, []);

  const loadRegression = useCallback(async (id: string) => {
    const ticket = ++regressionTicket.current;
    try {
      const regression = await getRegression(id);
      if (ticket === regressionTicket.current) dispatch({ type: "regressionLoaded", runId: id, regression });
    } catch {
      // 409 until RegressionCheck succeeds; the stage tracker already shows why
    }
  }, []);

  const disconnect = useCallback(() => {
    window.clearTimeout(reconnectTimer.current);
    const ws = socket.current;
    socket.current = null;
    if (ws) {
      ws.onclose = null;
      ws.close();
    }
  }, []);

  const connect = useCallback(
    (id: string) => {
      const ws = new WebSocket(eventsUrl(id, lastSeq.current));
      socket.current = ws;

      ws.onopen = () => {
        reconnectAttempts.current = 0;
      };
      ws.onmessage = (message) => {
        const event = JSON.parse(message.data as string) as RunEvent;
        if (event.run_id !== runId.current || event.seq <= lastSeq.current) return;
        lastSeq.current = event.seq;
        dispatch({ type: "event", event });
        if (event.type === "stage.status" && event.stage === "pipeline_architect" && event.status === "success") {
          void loadGraph(id);
        }
        if (event.type === "stage.status" && event.status === "success") {
          // The graph endpoint overlays findings onto node status, so refresh it along with the findings.
          if (event.stage === "diagnostic_sentinel") {
            void loadGraph(id);
            void loadDiagnostics(id);
          } else if (event.stage === "root_cause_diagnostician") {
            void loadDiagnostics(id);
          } else if (event.stage === "patch_master") {
            void loadPatches(id);
          } else if (event.stage === "regression_check") {
            void loadRegression(id);
            void loadGraph(id); // nodes whose findings were verified as resolved come back as "fixed"
          }
        }
        if (event.type === "run.status" && TERMINAL_RUN_STATUSES.has(event.status)) {
          finished.current = true;
        }
      };
      ws.onclose = (close) => {
        if (socket.current !== ws) return; // replaced by a newer run
        socket.current = null;
        if (close.code === WS_CLOSE_UNKNOWN_RUN) {
          dispatch({ type: "error", message: `Run ${id} no longer exists on the server (it may have restarted).` });
          return;
        }
        if (finished.current) return;
        // Dropped mid-run: resume from the last event we saw.
        const delay = Math.min(250 * 2 ** reconnectAttempts.current, MAX_RECONNECT_DELAY_MS);
        reconnectAttempts.current += 1;
        reconnectTimer.current = window.setTimeout(() => {
          if (runId.current === id) connect(id);
        }, delay);
      };
    },
    [loadGraph, loadDiagnostics, loadPatches, loadRegression],
  );

  const follow = useCallback(
    (id: string) => {
      disconnect();
      runId.current = id;
      lastSeq.current = 0;
      finished.current = false;
      reconnectAttempts.current = 0;
      setRunInUrl(id);
      connect(id);
    },
    [connect, disconnect],
  );

  const start = useCallback(
    async (source: string) => {
      disconnect();
      runId.current = null;
      dispatch({ type: "start", source });
      try {
        const created = await createRun(source);
        dispatch({ type: "created", runId: created.run_id, source: created.source });
        follow(created.run_id);
      } catch (err) {
        dispatch({ type: "error", message: err instanceof Error ? err.message : String(err) });
      }
    },
    [disconnect, follow],
  );

  const attach = useCallback(
    async (id: string) => {
      try {
        const run = await getRun(id);
        dispatch({ type: "attach", runId: run.run_id, source: run.source, status: run.status });
        follow(run.run_id); // the stream replays everything from seq 1
      } catch (err) {
        setRunInUrl(null);
        const message =
          err instanceof ApiError && err.status === 404
            ? `Run ${id} was not found. Runs live in server memory and are lost on restart.`
            : err instanceof Error
              ? err.message
              : String(err);
        dispatch({ type: "error", message });
      }
    },
    [follow],
  );

  const dismissError = useCallback(() => dispatch({ type: "dismissError" }), []);
  const selectNode = useCallback((nodeId: string | null) => dispatch({ type: "selectNode", nodeId }), []);

  // Reattach to ?run=<id> on load, so a refresh keeps the current run on screen.
  useEffect(() => {
    const id = new URL(window.location.href).searchParams.get("run");
    if (id) void attach(id);
    return disconnect;
  }, [attach, disconnect]);

  return { state, start, dismissError, selectNode };
}

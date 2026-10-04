import { describe, expect, it } from "vitest";

import type { RunEvent, StageName, StageStatus } from "../api/types";
import { MAX_LOG_LINES, initialRunState, runReducer, type RunState } from "./runReducer";

let seq = 0;
const at = (ms: number) => new Date(Date.UTC(2026, 9, 4, 12, 0, 0, ms)).toISOString();

function stage(name: StageName, status: StageStatus, extra: Partial<RunEvent> = {}, ms = 0): RunEvent {
  return {
    type: "stage.status",
    seq: ++seq,
    run_id: "r1",
    ts: at(ms),
    stage: name,
    status,
    attempt: status === "pending" ? 0 : 1,
    error: null,
    skipped_reason: null,
    ...extra,
  } as RunEvent;
}

function started(): RunState {
  seq = 0;
  const s = runReducer(initialRunState, { type: "start", source: "/repo" });
  return runReducer(s, { type: "created", runId: "r1", source: "/repo" });
}

const apply = (state: RunState, ...events: RunEvent[]) =>
  events.reduce((s, event) => runReducer(s, { type: "event", event }), state);

describe("runReducer", () => {
  it("tracks a stage through retry to success with timing", () => {
    const s = apply(
      started(),
      stage("repo_scout", "running", {}, 0),
      stage("repo_scout", "failed", { error: { stage: "repo_scout", attempt: 1, type: "RuntimeError", message: "boom", timed_out: false, traceback: null, at: at(100) } }, 100),
      stage("repo_scout", "running", { attempt: 2 }, 150),
      stage("repo_scout", "success", { attempt: 2 }, 400),
    );
    const view = s.stages.repo_scout;
    expect(view.status).toBe("success");
    expect(view.attempt).toBe(2);
    expect(view.error).toBeNull();
    expect(view.finishedAt! - view.startedAt!).toBe(400); // measured from the first attempt
    expect(s.logs.map((l) => l.message)).toEqual([
      "RepoScout running",
      "RepoScout failed: RuntimeError: boom",
      "RepoScout running (attempt 2)",
      "RepoScout success",
    ]);
  });

  it("keeps the escalation error and skip reasons", () => {
    const error = { stage: "patch_master" as const, attempt: 2, type: "TimeoutError", message: "timed out after 300s", timed_out: true, traceback: null, at: at(0) };
    const s = apply(
      started(),
      stage("patch_master", "escalated", { attempt: 2, error }),
      stage("regression_check", "skipped", { attempt: 0, skipped_reason: "required stage did not succeed: PatchMaster" }),
    );
    expect(s.stages.patch_master.error?.timed_out).toBe(true);
    expect(s.stages.regression_check.skippedReason).toMatch(/PatchMaster/);
  });

  it("ignores replayed and foreign events", () => {
    const base = started();
    const first = stage("repo_scout", "running");
    let s = apply(base, first);
    s = apply(s, first, { ...stage("system_analyst", "running"), run_id: "other" } as RunEvent);
    expect(s.lastSeq).toBe(1);
    expect(s.stages.system_analyst.status).toBe("pending");
  });

  it("records run status, start and finish", () => {
    const s = apply(
      started(),
      { type: "run.status", seq: ++seq, run_id: "r1", ts: at(0), status: "running", detail: null },
      { type: "run.status", seq: ++seq, run_id: "r1", ts: at(2500), status: "failed", detail: "cancelled" },
    );
    expect(s.status).toBe("failed");
    expect(s.finishedAt! - s.startedAt!).toBe(2500);
    expect(s.logs.at(-1)?.message).toBe("Run failed: cancelled");
  });

  it("caps the log buffer", () => {
    let s = started();
    for (let i = 0; i < MAX_LOG_LINES + 10; i++) {
      s = apply(s, { type: "log", seq: ++seq, run_id: "r1", ts: at(0), stage: null, level: "info", message: `line ${i}` });
    }
    expect(s.logs).toHaveLength(MAX_LOG_LINES);
    expect(s.logs[0].message).toBe("line 10");
  });

  it("drops graph results that arrive for a previous run", () => {
    const s = runReducer(started(), { type: "graphLoaded", runId: "old", graph: { nodes: [], edges: [], columns: [] } });
    expect(s.graph).toBeNull();
  });

  it("keeps diagnostics for the current run only and tracks the selected node", () => {
    const diagnostics = { findings: [], sources: [], root_causes: [], root_causes_ready: true };
    let s = runReducer(started(), { type: "diagnosticsLoaded", runId: "old", diagnostics });
    expect(s.diagnostics).toBeNull(); // a response for a previous run is dropped
    s = runReducer(s, { type: "diagnosticsLoaded", runId: "r1", diagnostics });
    expect(s.diagnostics).toBe(diagnostics);

    s = runReducer(s, { type: "selectNode", nodeId: "fn:a" });
    expect(s.selectedNodeId).toBe("fn:a");
    expect(runReducer(s, { type: "selectNode", nodeId: "fn:a" })).toBe(s); // no needless re-render
    expect(runReducer(s, { type: "start", source: "/next" }).selectedNodeId).toBeNull(); // a new run clears it
    expect(runReducer(s, { type: "start", source: "/next" }).diagnostics).toBeNull();
  });

  it("keeps patches for the current run only", () => {
    const patches = [
      { id: "P-1", root_cause_id: "RC-1", status: "valid" as const, detail: null, diff: "", files: [], design_suggestion: "", attempts: 1, rationale: "", memory_examples: 0 },
    ];
    let s = runReducer(started(), { type: "patchesLoaded", runId: "old", patches });
    expect(s.patches).toBeNull();
    s = runReducer(s, { type: "patchesLoaded", runId: "r1", patches });
    expect(s.patches).toBe(patches);
    expect(runReducer(s, { type: "start", source: "/next" }).patches).toBeNull();
  });

  it("keeps the regression result for the current run only", () => {
    const regression = { baseline_totals: null, verdicts: [], summary: null };
    let s = runReducer(started(), { type: "regressionLoaded", runId: "old", regression });
    expect(s.regression).toBeNull();
    s = runReducer(s, { type: "regressionLoaded", runId: "r1", regression });
    expect(s.regression).toBe(regression);
    expect(runReducer(s, { type: "start", source: "/next" }).regression).toBeNull();
  });

  it("returns to idle when a run cannot be created", () => {
    const s = runReducer(runReducer(initialRunState, { type: "start", source: "nope" }), { type: "error", message: "Not a git URL" });
    expect(s.status).toBe("idle");
    expect(s.error).toBe("Not a git URL");
  });
});

// Mirrors the backend's Pydantic models (backend/app/models). Keep in sync.

export type StageName =
  | "repo_scout"
  | "system_analyst"
  | "pipeline_architect"
  | "sandbox_runner"
  | "diagnostic_sentinel"
  | "root_cause_diagnostician"
  | "patch_master"
  | "regression_check"
  | "memory_keeper";

export type StageStatus = "pending" | "running" | "success" | "failed" | "escalated" | "skipped";

export type RunStatus = "queued" | "running" | "completed" | "completed_with_escalations" | "failed";

export const TERMINAL_RUN_STATUSES: ReadonlySet<RunStatus> = new Set([
  "completed",
  "completed_with_escalations",
  "failed",
]);

export type LogLevel = "debug" | "info" | "warning" | "error";

export interface StageError {
  stage: StageName | null;
  attempt: number;
  type: string;
  message: string;
  timed_out: boolean;
  traceback: string | null;
  at: string;
}

interface EventBase {
  seq: number;
  run_id: string;
  ts: string;
}

export interface RunStatusEvent extends EventBase {
  type: "run.status";
  status: RunStatus;
  detail: string | null;
}

export interface StageStatusEvent extends EventBase {
  type: "stage.status";
  stage: StageName;
  status: StageStatus;
  attempt: number;
  error: StageError | null;
  skipped_reason: string | null;
}

export interface LogEvent extends EventBase {
  type: "log";
  stage: StageName | null;
  level: LogLevel;
  message: string;
}

export type RunEvent = RunStatusEvent | StageStatusEvent | LogEvent;

export type SourceKind = "git" | "local";

export interface CreateRunResponse {
  run_id: string;
  status: RunStatus;
  source: string;
  source_kind: SourceKind;
  events_url: string;
}

export interface RunSummary {
  run_id: string;
  source: string;
  source_kind: SourceKind;
  status: RunStatus;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
}

// --- Graph ---------------------------------------------------------------

export type NodeType = "entry" | "function" | "service" | "database" | "external";
export type EdgeKind = "call" | "data" | "import";
export type Layer = "entry" | "api" | "service" | "data" | "util" | "test";
export type NodeStatus = "idle" | "healthy" | "warning" | "failing" | "patched" | "fixed";

export interface NodeMetrics {
  loc: number | null;
  complexity: number | null;
  fan_in: number;
  fan_out: number;
  call_sites: number;
  findings: number;
  /** Most earlier runs in which one of this node's patterns was seen. */
  seen_before: number;
}

export interface GraphNode {
  id: string;
  label: string;
  type: NodeType;
  file: string | null;
  start_line: number | null;
  end_line: number | null;
  layer: Layer | null;
  status: NodeStatus;
  metrics: NodeMetrics;
  position: { x: number; y: number };
  symbol: string | null;
}

export interface GraphEdge {
  id: string;
  source: string;
  target: string;
  kind: EdgeKind;
  animated: boolean;
}

export interface GraphColumn {
  key: string;
  title: string;
  x: number;
}

export interface Graph {
  nodes: GraphNode[];
  edges: GraphEdge[];
  columns: GraphColumn[];
}

// --- Diagnostics -----------------------------------------------------------

export type Severity = "info" | "low" | "medium" | "high" | "critical";
export type FindingCategory = "exception" | "race_condition" | "security";
export type FindingSource = "sandbox" | "bandit" | "static";
export type SourceStatus = "ok" | "skipped" | "failed";
export type ChainRole = "entry" | "path" | "offender";
export type ExplanationSource = "claude" | "template";

export interface SourceLocation {
  file: string;
  line: number;
  end_line: number | null;
  column: number | null;
}

export interface StackFrame {
  file: string;
  line: number;
  function: string;
}

export interface ExceptionInfo {
  type: string;
  message: string;
  frames: StackFrame[];
}

export interface Finding {
  id: string;
  category: FindingCategory;
  source: FindingSource;
  rule_id: string;
  title: string;
  severity: Severity;
  location: SourceLocation;
  node_id: string | null;
  evidence: string;
  owasp: string | null;
  owasp_name: string | null;
  exception: ExceptionInfo | null;
  source_test: string | null;
  /** Hash of exception type (or rule), normalized code pattern and OWASP category; null in test code. */
  signature: string | null;
  pattern: string | null;
  /** In how many earlier runs this signature was already recorded. */
  seen_before: number;
  /** The severity before it was raised for a pattern seen before. */
  base_severity: Severity | null;
}

export interface SourceReport {
  source: FindingSource;
  status: SourceStatus;
  findings: number;
  detail: string | null;
}

export interface ChainStep {
  node_id: string;
  label: string;
  file: string | null;
  line: number | null;
  role: ChainRole;
  via: string;
}

export interface CodeSnippet {
  file: string;
  start_line: number;
  highlight_line: number;
  code: string;
}

export interface RootCause {
  id: string;
  finding_ids: string[];
  location: SourceLocation;
  node_id: string | null;
  symbol: string | null;
  chain: ChainStep[];
  snippet: CodeSnippet | null;
  explanation: string;
  explanation_source: ExplanationSource;
  confidence: number;
  evidence: string[];
}

export interface Diagnostics {
  findings: Finding[];
  sources: SourceReport[];
  root_causes: RootCause[];
  root_causes_ready: boolean;
}

// --- Patches ---------------------------------------------------------------

export type PatchStatus = "valid" | "invalid" | "skipped";

export interface PatchedFile {
  path: string;
  original: string;
  patched: string;
}

export interface Patch {
  id: string;
  root_cause_id: string;
  status: PatchStatus;
  /** Why a patch is invalid or skipped. */
  detail: string | null;
  diff: string;
  files: PatchedFile[];
  design_suggestion: string;
  /** Requests made to Claude: 1, or 2 when the first diff did not apply. */
  attempts: number;
  rationale: string;
  /** Past accepted fixes for similar incidents that were shown to Claude. */
  memory_examples: number;
}

// --- Incident memory -------------------------------------------------------

export interface Incident {
  id: number;
  signature: string;
  run_id: string;
  source: string;
  created_at: string;
  kind: string;
  pattern: string;
  owasp: string | null;
  category: string;
  severity: Severity;
  rule_id: string;
  title: string;
  file: string;
  line: number;
  symbol: string | null;
  root_cause: string | null;
  patch_status: string;
  patch_diff: string | null;
  patch_design: string | null;
  regression_passed: boolean | null;
  regression_reasons: string[];
  /** Incidents sharing this signature, across all runs. */
  occurrences: number;
}

export interface Incidents {
  enabled: boolean;
  total: number;
  incidents: Incident[];
}

// --- Regression check ------------------------------------------------------

export type Verdict = "pass" | "regressed" | "apply_failed";

export interface SuiteTotals {
  passed: number;
  failed: number;
  errors: number;
  skipped: number;
}

export interface FindingRef {
  finding_id: string;
  rule_id: string;
  title: string;
  severity: Severity;
  location: SourceLocation;
  node_id: string | null;
}

export interface PatchVerdict {
  patch_id: string;
  root_cause_id: string;
  verdict: Verdict;
  accepted: boolean;
  /** Why a patch was rejected, or notes on an accepted one. */
  reasons: string[];
  totals: SuiteTotals;
  newly_failing: string[];
  newly_passing: string[];
  findings_resolved: FindingRef[];
  findings_introduced: FindingRef[];
  detail: string | null;
}

export interface RegressionSummary {
  patches_applied: string[];
  tests_before: SuiteTotals;
  tests_after: SuiteTotals;
  tests_fixed: string[];
  tests_broken: string[];
  findings_before: number;
  findings_after: number;
  findings_resolved: FindingRef[];
  findings_introduced: FindingRef[];
}

export interface RegressionCheckOutput {
  baseline_totals: SuiteTotals | null;
  verdicts: PatchVerdict[];
  summary: RegressionSummary | null;
}

// --- GitHub pull request ---------------------------------------------------

export interface PullRequestBlocker {
  /** e.g. "github_token_missing", "github_repo_unknown". */
  code: string;
  message: string;
}

export interface PullRequestResult {
  number: number;
  url: string;
  repo: string;
  branch: string;
  base: string;
  title: string;
  commits: number;
  draft: boolean;
  /** False when a pull request for this run already existed and nothing new was made. */
  created: boolean;
  notes: string[];
}

export interface PullRequestStatus {
  ready: boolean;
  blocker: PullRequestBlocker | null;
  repo: string | null;
  branch: string;
  base: string | null;
  title: string;
  patch_ids: string[];
  pull_request: PullRequestResult | null;
}

// --- Environment -----------------------------------------------------------

export interface Environment {
  /** The bundled demo app, when it ships next to the backend. */
  demo_path: string | null;
  /** Claude can explain root causes and write patches. */
  ai: boolean;
  /** GITHUB_TOKEN is set, so pull requests can be created. */
  github: boolean;
  /** Incidents are remembered between runs. */
  memory: boolean;
}

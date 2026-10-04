import type { FindingCategory, FindingSource, Layer, NodeType, Severity, StageName } from "./api/types";

export interface StageInfo {
  name: StageName;
  title: string;
  description: string;
}

/** The nine agents, in execution order. */
export const STAGES: StageInfo[] = [
  { name: "repo_scout", title: "RepoScout", description: "Fetch the repo and index symbols" },
  { name: "system_analyst", title: "SystemAnalyst", description: "Detect stack and layers" },
  { name: "pipeline_architect", title: "PipelineArchitect", description: "Build the data-flow graph" },
  { name: "sandbox_runner", title: "SandboxRunner", description: "Run tests and probes in isolation" },
  { name: "diagnostic_sentinel", title: "DiagnosticSentinel", description: "Find exceptions, races, OWASP issues" },
  { name: "root_cause_diagnostician", title: "RootCauseDiagnostician", description: "Trace failures to file and line" },
  { name: "patch_master", title: "PatchMaster", description: "Generate unified diffs" },
  { name: "regression_check", title: "RegressionCheck", description: "Re-run tests with the patch" },
  { name: "memory_keeper", title: "MemoryKeeper", description: "Store incident signatures" },
];

/** What each agent does, in a sentence, for the landing page. */
export const STAGE_DETAILS: Record<StageName, string> = {
  repo_scout: "Clones or copies the repo and parses every Python file into a symbol index: modules, classes, functions and who calls whom.",
  system_analyst: "Detects the stack from dependencies and imports, and sorts each module into entry, API, service, data or util.",
  pipeline_architect: "Turns the index into the node-and-edge graph you see here, laid out by layer so data flow reads left to right.",
  sandbox_runner: "Runs the repo's own tests in a throwaway virtualenv with no network and a timeout, and records every call.",
  diagnostic_sentinel: "Collects exceptions from the sandbox, Bandit findings mapped to OWASP, and unlocked shared state in threaded code.",
  root_cause_diagnostician: "Maps each finding to a graph node and builds the causal chain from the entry point to the offending line.",
  patch_master: "Asks Claude for a minimal unified diff and a hardened design note, and checks it applies with git apply.",
  regression_check: "Applies the patches to a fresh copy, re-runs tests and scans, and rejects any patch that breaks something.",
  memory_keeper: "Stores each incident's signature in SQLite so repeats are flagged and past fixes guide the next patch.",
};

export interface StagePhase {
  key: string;
  title: string;
  blurb: string;
  stages: StageName[];
}

/** The nine agents in three acts. */
export const PHASES: StagePhase[] = [
  { key: "understand", title: "Understand", blurb: "Read the code", stages: ["repo_scout", "system_analyst", "pipeline_architect"] },
  { key: "observe", title: "Observe", blurb: "Run it and find what breaks", stages: ["sandbox_runner", "diagnostic_sentinel", "root_cause_diagnostician"] },
  { key: "fix", title: "Fix and learn", blurb: "Patch, verify, remember", stages: ["patch_master", "regression_check", "memory_keeper"] },
];

export const STAGE_TITLES = Object.fromEntries(STAGES.map((s) => [s.name, s.title])) as Record<StageName, string>;

export const LAYER_COLORS: Record<Layer, string> = {
  entry: "#b394ff",
  api: "#4cc2ff",
  service: "#3ddc97",
  data: "#ffb547",
  util: "#94a0b8",
  test: "#ff7ab6",
};

export const TYPE_COLORS: Partial<Record<NodeType, string>> = {
  database: "#ff8a4c",
  external: "#7c8aa5",
};

export function nodeColor(type: NodeType, layer: Layer | null): string {
  return TYPE_COLORS[type] ?? (layer ? LAYER_COLORS[layer] : LAYER_COLORS.util);
}

export const SEVERITY_COLORS: Record<Severity, string> = {
  critical: "#ff4d6a",
  high: "#ff6370",
  medium: "#ffb547",
  low: "#6d8bff",
  info: "#7d869a",
};

export const CATEGORY_LABELS: Record<FindingCategory, string> = {
  exception: "Exception",
  race_condition: "Race condition",
  security: "Security",
};

export const SOURCE_LABELS: Record<FindingSource, string> = {
  sandbox: "Sandbox",
  bandit: "Bandit",
  static: "Static check",
};

export const LAYER_LABELS: Record<Layer, string> = {
  entry: "Entry",
  api: "API",
  service: "Service",
  data: "Data",
  util: "Util",
  test: "Test",
};

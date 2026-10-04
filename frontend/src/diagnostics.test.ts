import { describe, expect, it } from "vitest";

import type { ChainStep, Diagnostics, Finding, Graph, RootCause } from "./api/types";
import { causesForNode, chainEdgeIds, chainNodeIds, findingsForNode, tokenizeLine, worstSeverity } from "./diagnostics";

const step = (node_id: string, role: ChainStep["role"] = "path"): ChainStep => ({
  node_id,
  label: node_id,
  file: "a.py",
  line: 1,
  role,
  via: "graph",
});

const location = { file: "a.py", line: 1, end_line: null, column: null };

const finding = (id: string, node_id: string | null, severity: Finding["severity"] = "high"): Finding => ({
  id,
  category: "security",
  source: "bandit",
  rule_id: "B608",
  title: id,
  severity,
  location,
  node_id,
  evidence: "",
  owasp: null,
  owasp_name: null,
  exception: null,
  source_test: null,
  signature: null,
  pattern: null,
  seen_before: 0,
  base_severity: null,
});

const cause = (id: string, node_id: string, chain: ChainStep[]): RootCause => ({
  id,
  finding_ids: [],
  location,
  node_id,
  symbol: null,
  chain,
  snippet: null,
  explanation: "",
  explanation_source: "template",
  confidence: 0.5,
  evidence: [],
});

const graph: Graph = {
  nodes: [],
  columns: [],
  edges: [
    { id: "e1", source: "main", target: "handle", kind: "call", animated: false },
    { id: "e2", source: "handle", target: "save", kind: "call", animated: false },
    { id: "e3", source: "main", target: "other", kind: "call", animated: false },
    { id: "e4", source: "save", target: "db", kind: "data", animated: true },
  ],
};

const diagnostics: Diagnostics = {
  findings: [finding("f1", "save"), finding("f2", "other", "low"), finding("f3", null)],
  sources: [],
  root_causes_ready: true,
  root_causes: [
    cause("c1", "save", [step("main", "entry"), step("handle"), step("save", "offender")]),
    cause("c2", "other", [step("main", "entry"), step("other", "offender")]),
  ],
};

describe("diagnostics helpers", () => {
  it("finds findings and root causes by node", () => {
    expect(findingsForNode(diagnostics, "save").map((f) => f.id)).toEqual(["f1"]);
    expect(causesForNode(diagnostics, "other").map((c) => c.id)).toEqual(["c2"]);
    expect(findingsForNode(null, "save")).toEqual([]);
  });

  it("emphasizes every chain until a node is selected, then only its own", () => {
    expect([...chainNodeIds(diagnostics, null)].sort()).toEqual(["handle", "main", "other", "save"]);
    expect([...chainNodeIds(diagnostics, "save")].sort()).toEqual(["handle", "main", "save"]);
    expect([...chainEdgeIds(graph, diagnostics, "save")].sort()).toEqual(["e1", "e2"]);
    // never the data edge into the database: it is not part of any chain
    expect([...chainEdgeIds(graph, diagnostics, null)].sort()).toEqual(["e1", "e2", "e3"]);
    expect(chainNodeIds(null, null).size).toBe(0);
  });

  it("picks the worst severity", () => {
    expect(worstSeverity([finding("a", "n", "low"), finding("b", "n", "critical"), finding("c", "n", "medium")])).toBe("critical");
    expect(worstSeverity([])).toBeNull();
  });
});

describe("tokenizeLine", () => {
  const kinds = (line: string) => tokenizeLine(line).filter((t) => t.kind !== "plain").map((t) => [t.kind, t.text]);

  it("separates keywords, strings, numbers, comments and decorators", () => {
    expect(kinds("    return db.fetch(n) // 2  # half")).toEqual([
      ["keyword", "return"],
      ["number", "2"],
      ["comment", "# half"],
    ]);
    expect(kinds('@app.get("/x")')).toEqual([
      ["decorator", "@app.get"],
      ["string", '"/x"'],
    ]);
    expect(kinds("q = f\"SELECT {name}\" if True else 'x'")).toEqual([
      ["string", 'f"SELECT {name}"'],
      ["keyword", "if"],
      ["keyword", "True"],
      ["keyword", "else"],
      ["string", "'x'"],
    ]);
  });

  it("never drops or reorders text", () => {
    for (const line of ["", "    self.x[k] = current - 1  # it's fine", 'print("unterminated', "x = 'a' + \"b\" + 3.14"]) {
      expect(tokenizeLine(line).map((t) => t.text).join("")).toBe(line);
    }
  });
});

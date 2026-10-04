import type { ChainStep, Diagnostics, Finding, Graph, RootCause, Severity } from "./api/types";

/** Most severe first. */
export const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low", "info"];

export function findingsForNode(d: Diagnostics | null, nodeId: string): Finding[] {
  return d ? d.findings.filter((f) => f.node_id === nodeId) : [];
}

/** Root causes whose offending line sits in the node. */
export function causesForNode(d: Diagnostics | null, nodeId: string): RootCause[] {
  return d ? d.root_causes.filter((c) => c.node_id === nodeId) : [];
}

/** Chains to emphasize: the selected node's, or every chain when nothing is selected. */
function relevantChains(d: Diagnostics | null, selectedNodeId: string | null): ChainStep[][] {
  if (!d) return [];
  const causes = selectedNodeId ? causesForNode(d, selectedNodeId) : d.root_causes;
  return causes.map((c) => c.chain).filter((chain) => chain.length > 0);
}

export function chainNodeIds(d: Diagnostics | null, selectedNodeId: string | null): Set<string> {
  return new Set(relevantChains(d, selectedNodeId).flatMap((chain) => chain.map((s) => s.node_id)));
}

/** Graph edges joining consecutive chain steps, so the whole path lights up, not just its nodes. */
export function chainEdgeIds(graph: Graph, d: Diagnostics | null, selectedNodeId: string | null): Set<string> {
  const pairs = new Set<string>();
  for (const chain of relevantChains(d, selectedNodeId)) {
    for (let i = 0; i + 1 < chain.length; i++) pairs.add(`${chain[i].node_id}|${chain[i + 1].node_id}`);
  }
  return new Set(graph.edges.filter((e) => pairs.has(`${e.source}|${e.target}`)).map((e) => e.id));
}

export function worstSeverity(findings: Finding[]): Severity | null {
  return SEVERITY_ORDER.find((s) => findings.some((f) => f.severity === s)) ?? null;
}

// --- Minimal Python highlighting for the code panel ---------------------------------

export type TokenKind = "comment" | "string" | "keyword" | "number" | "decorator" | "plain";
export interface Token {
  kind: TokenKind;
  text: string;
}

const KEYWORDS = new Set(
  (
    "and as assert async await break class continue def del elif else except finally for from global " +
    "if import in is lambda nonlocal not or pass raise return try while with yield None True False self"
  ).split(" "),
);

// comment | string (optional prefix, may be unterminated) | decorator | number | identifier
const TOKEN =
  /(#.*$)|([rbfRBF]{0,2}(?:"(?:\\.|[^"\\])*"?|'(?:\\.|[^'\\])*'?))|(@[\w.]+)|(\b\d[\d_]*(?:\.\d+)?\b)|(\b[A-Za-z_]\w*\b)/g;

/** Splits one line into tokens. Enough for reading a snippet; it is not a parser (no multi-line strings). */
export function tokenizeLine(line: string): Token[] {
  const tokens: Token[] = [];
  let last = 0;
  for (const m of line.matchAll(TOKEN)) {
    const start = m.index ?? 0;
    if (start > last) tokens.push({ kind: "plain", text: line.slice(last, start) });
    const kind: TokenKind = m[1]
      ? "comment"
      : m[2]
        ? "string"
        : m[3]
          ? "decorator"
          : m[4]
            ? "number"
            : KEYWORDS.has(m[5])
              ? "keyword"
              : "plain";
    tokens.push({ kind, text: m[0] });
    last = start + m[0].length;
  }
  if (last < line.length) tokens.push({ kind: "plain", text: line.slice(last) });
  return tokens;
}

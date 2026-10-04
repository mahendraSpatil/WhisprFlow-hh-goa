"""Findings and verification results projected onto the graph: which nodes fail, and which are fixed."""

from __future__ import annotations

from collections import defaultdict

from app.models.agents import Finding, Graph, NodeStatus, RegressionCheckOutput, Severity

FAILING = {Severity.CRITICAL, Severity.HIGH}


def overlay_findings(graph: Graph, findings: list[Finding], regression: RegressionCheckOutput | None = None) -> Graph:
    """A copy of the graph where nodes with findings are `failing` (high or critical) or `warning`.

    A node whose findings were all resolved by the accepted patches, as measured by RegressionCheck
    on a fresh copy of the repo, is `fixed`.
    """
    resolved = {r.finding_id for r in regression.summary.findings_resolved} if regression and regression.summary else set()
    by_node: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        if f.node_id:
            by_node[f.node_id].append(f)
    nodes = []
    for node in graph.nodes:
        found = by_node.get(node.id)
        if not found:
            nodes.append(node)
            continue
        if all(f.id in resolved for f in found):
            status = NodeStatus.FIXED
        elif any(f.severity in FAILING for f in found):
            status = NodeStatus.FAILING
        else:
            status = NodeStatus.WARNING
        metrics = node.metrics.model_copy(update={"findings": len(found), "seen_before": max(f.seen_before for f in found)})
        nodes.append(node.model_copy(update={"status": status, "metrics": metrics}))
    return graph.model_copy(update={"nodes": nodes})

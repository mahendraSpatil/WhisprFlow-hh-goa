from __future__ import annotations

import asyncio
import hashlib
import os
from collections import defaultdict
from pathlib import Path

from app.agents.base import Agent
from app.agents.diagnostic_sentinel import SEVERITY_ORDER
from app.agents.explainers import (
    ClaudeExplainer,
    ExplainRequest,
    Explainer,
    template_explanation,
)
from app.agents.graph_index import NodeIndex
from app.models.agents import (
    CallResolution,
    ChainRole,
    ChainStep,
    CodeSnippet,
    DiagnosticSentinelOutput,
    ExplanationSource,
    Finding,
    FindingSource,
    GraphNode,
    NodeType,
    PipelineArchitectOutput,
    RepoScoutOutput,
    RootCause,
    RootCauseDiagnosticianInput,
    RootCauseDiagnosticianOutput,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger

SNIPPET_CONTEXT_LINES = 6
MAX_SNIPPET_FILE_BYTES = 1_000_000
MAX_EXPLAINED_BY_CLAUDE = 20  # one request per root cause; the rest get the template
CLAUDE_CONCURRENCY = 4
BASE_CONFIDENCE = {FindingSource.SANDBOX: 0.9, FindingSource.BANDIT: 0.7, FindingSource.STATIC: 0.6}


class RootCauseDiagnostician(Agent[RootCauseDiagnosticianInput, RootCauseDiagnosticianOutput]):
    """Traces each finding to the file and line behind it and explains why it goes wrong.

    Findings at the same location share one root cause. For each, the traceback frames (or, for
    static findings, the call graph) give the chain from an entry node to the offending line;
    that chain and the code around the line go to Claude for a short explanation. Without
    credentials, or when Claude declines, a deterministic explanation is used and labeled as such.
    Set CODELOOP_LLM=off to never send code to the API.
    """

    name = StageName.ROOT_CAUSE_DIAGNOSTICIAN
    title = "RootCauseDiagnostician"
    output_model = RootCauseDiagnosticianOutput
    requires = (StageName.REPO_SCOUT, StageName.DIAGNOSTIC_SENTINEL)
    uses = (StageName.PIPELINE_ARCHITECT,)
    default_timeout_s = 300.0

    def __init__(self, timeout_s: float | None = None, explainer: Explainer | None = None) -> None:
        super().__init__(timeout_s)
        self._explainer = explainer

    def build_input(self, ctx: RunContext) -> RootCauseDiagnosticianInput:
        architect = ctx.optional(StageName.PIPELINE_ARCHITECT, PipelineArchitectOutput)
        return RootCauseDiagnosticianInput(
            root=ctx.require_workspace(),
            repo=ctx.require(StageName.REPO_SCOUT, RepoScoutOutput),
            findings=ctx.require(StageName.DIAGNOSTIC_SENTINEL, DiagnosticSentinelOutput).findings,
            graph=architect.graph if architect else None,
        )

    async def run(self, inp: RootCauseDiagnosticianInput, log: StageLogger) -> RootCauseDiagnosticianOutput:
        if not inp.findings:
            log.info("No findings to trace")
            return RootCauseDiagnosticianOutput()
        if inp.graph is None:
            log.warning("No graph: causal chains from entry points are unavailable")

        explainer, owned = self._explainer, None
        if explainer is None and os.environ.get("CODELOOP_LLM", "on").lower() not in ("off", "0", "false"):
            explainer = owned = ClaudeExplainer(log)
        elif explainer is None:
            log.info("CODELOOP_LLM=off: using template explanations")
        try:
            causes = await diagnose(inp, explainer, log)
        finally:
            if owned is not None:
                await owned.aclose()
        from_claude = sum(c.explanation_source is ExplanationSource.CLAUDE for c in causes)
        log.info(f"{len(causes)} root causes for {len(inp.findings)} findings; {from_claude} explained by Claude")
        return RootCauseDiagnosticianOutput(root_causes=causes)


# --- Diagnosis -----------------------------------------------------------------------


async def diagnose(inp: RootCauseDiagnosticianInput, explainer: Explainer | None, log: StageLogger) -> list[RootCause]:
    index = NodeIndex(inp.graph)
    builder = ChainBuilder(index, inp.repo)

    groups: dict[tuple[str, int], list[Finding]] = defaultdict(list)
    for f in inp.findings:
        groups[(f.location.file, f.location.line)].append(f)

    requests: list[ExplainRequest] = []
    drafts: list[tuple[list[Finding], GraphNode | None, list[ChainStep]]] = []
    for findings in sorted(groups.values(), key=lambda g: (min(SEVERITY_ORDER[f.severity] for f in g), g[0].location.file, g[0].location.line)):
        findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.rule_id))
        location = findings[0].location
        node = next((index.nodes[f.node_id] for f in findings if f.node_id in index.nodes), None) or index.at(
            location.file, location.line
        )
        chain = builder.chain_for(findings, node)
        symbol = node.symbol if node and node.type is not NodeType.DATABASE else None
        snippet = read_snippet(inp.root, location.file, location.line)
        requests.append(ExplainRequest(findings, location, symbol, chain, snippet))
        drafts.append((findings, node, chain))

    gate = asyncio.Semaphore(CLAUDE_CONCURRENCY)

    async def explain(position: int, request: ExplainRequest) -> str | None:
        if explainer is None or position >= MAX_EXPLAINED_BY_CLAUDE:
            return None
        async with gate:
            return await explainer.explain(request)

    if explainer is not None and len(requests) > MAX_EXPLAINED_BY_CLAUDE:
        log.warning(f"Only the {MAX_EXPLAINED_BY_CLAUDE} most severe root causes are sent to Claude")
    texts = await asyncio.gather(*(explain(i, r) for i, r in enumerate(requests)))

    causes = []
    for (findings, node, chain), request, text in zip(drafts, requests, texts):
        top = findings[0]
        reached_from_entry = len(chain) > 1 and chain[0].role is ChainRole.ENTRY
        causes.append(
            RootCause(
                id="RC-" + hashlib.sha1(f"{request.location.file}:{request.location.line}".encode()).hexdigest()[:8],
                finding_ids=[f.id for f in findings],
                location=request.location,
                node_id=node.id if node else None,
                symbol=request.symbol,
                chain=chain,
                snippet=request.snippet,
                explanation=text or template_explanation(request),
                explanation_source=ExplanationSource.CLAUDE if text else ExplanationSource.TEMPLATE,
                confidence=min(0.95, BASE_CONFIDENCE[top.source] + (0.05 if reached_from_entry else 0.0)),
                evidence=[f.evidence for f in findings],
            )
        )
    return causes


class ChainBuilder:
    """Builds the chain from an entry node to the node holding the offending line."""

    def __init__(self, index: NodeIndex, repo: RepoScoutOutput) -> None:
        self.index = index
        self.call_lines: dict[tuple[str, str], int] = {}
        for call in repo.calls:
            if call.resolution is CallResolution.INTERNAL and call.target:
                self.call_lines.setdefault((call.caller, call.target), call.location.line)

    def chain_for(self, findings: list[Finding], offender: GraphNode | None) -> list[ChainStep]:
        if offender is None:
            return []
        location = findings[0].location
        traced = self._from_traceback(findings)
        if traced:
            steps = traced
            if steps[-1].node_id != offender.id:
                steps.append(self._step(offender, location.line, "traceback"))
        else:
            steps = [self._step(offender, location.line, "graph")]
        first = self.index.nodes[steps[0].node_id]
        if first.type is not NodeType.ENTRY:
            path = self.index.entry_path(first.id)
            if path and len(path) > 1:
                steps = [self._path_step(path[i], path[i + 1]) for i in range(len(path) - 1)] + steps
        return self._assign_roles(steps)

    def _from_traceback(self, findings: list[Finding]) -> list[ChainStep]:
        exception = next((f.exception for f in findings if f.exception and f.exception.frames), None)
        if exception is None:
            return []
        steps: list[ChainStep] = []
        for frame in exception.frames:  # outermost first
            node = self.index.by_symbol(frame.function) or self.index.at(frame.file, frame.line)
            if node is None or (steps and steps[-1].node_id == node.id):
                continue
            steps.append(self._step(node, frame.line, "traceback"))
        return steps

    def _step(self, node: GraphNode, line: int | None, via: str) -> ChainStep:
        return ChainStep(
            node_id=node.id, label=node.label, file=node.file, line=line or node.start_line, role=ChainRole.PATH, via=via
        )

    def _path_step(self, node: GraphNode, nxt: GraphNode) -> ChainStep:
        line = self.call_lines.get((node.symbol or "", nxt.symbol or ""))
        return self._step(node, line, "graph")

    def _assign_roles(self, steps: list[ChainStep]) -> list[ChainStep]:
        if not steps:
            return steps
        steps[-1] = steps[-1].model_copy(update={"role": ChainRole.OFFENDER})
        if len(steps) > 1 and self.index.nodes[steps[0].node_id].type is NodeType.ENTRY:
            steps[0] = steps[0].model_copy(update={"role": ChainRole.ENTRY})
        return steps


def read_snippet(root: Path, file: str, line: int) -> CodeSnippet | None:
    """Lines around ``line``. Refuses paths that resolve outside the repo copy."""
    try:
        base = root.resolve()
        path = (base / file).resolve()
        if base not in path.parents or not path.is_file() or path.stat().st_size > MAX_SNIPPET_FILE_BYTES:
            return None
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    if not 1 <= line <= len(lines):
        return None
    start = max(1, line - SNIPPET_CONTEXT_LINES)
    end = min(len(lines), line + SNIPPET_CONTEXT_LINES)
    return CodeSnippet(file=file, start_line=start, highlight_line=line, code="\n".join(lines[start - 1 : end]))

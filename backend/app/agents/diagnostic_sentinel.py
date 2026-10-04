from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable

from app.agents.bandit_scan import run_bandit
from app.agents.base import Agent
from app.agents.graph_index import NodeIndex
from app.agents.owasp import owasp_name
from app.agents.race_check import describe, find_races
from app.agents.sandbox_findings import collect_failures
from app.memory import MemoryStore, attach_signatures, boosted, open_memory
from app.models.agents import (
    DEFAULT_EXCLUDE_DIRS,
    DiagnosticSentinelInput,
    DiagnosticSentinelOutput,
    ExceptionInfo,
    Finding,
    FindingCategory,
    FindingSource,
    PipelineArchitectOutput,
    RepoScoutOutput,
    SandboxRunnerOutput,
    Severity,
    SourceLocation,
    SourceReport,
    SourceStatus,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger

SEVERITY_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2, Severity.LOW: 3, Severity.INFO: 4}
SQL_SYNTAX_OWASP = "A03:2021"


class DiagnosticSentinel(Agent[DiagnosticSentinelInput, DiagnosticSentinelOutput]):
    """Collects failures from three independent sources and rates each one.

    - sandbox: exceptions and tracebacks from failing tests and probes
    - bandit: static security scan, mapped to OWASP Top 10 categories
    - static: shared state modified by threaded code without a lock

    A source that cannot run is reported and skipped; the stage only fails when none of them ran.
    """

    name = StageName.DIAGNOSTIC_SENTINEL
    title = "DiagnosticSentinel"
    output_model = DiagnosticSentinelOutput
    requires = (StageName.REPO_SCOUT,)
    uses = (StageName.SANDBOX_RUNNER, StageName.PIPELINE_ARCHITECT)
    default_timeout_s = 300.0

    def build_input(self, ctx: RunContext) -> DiagnosticSentinelInput:
        architect = ctx.optional(StageName.PIPELINE_ARCHITECT, PipelineArchitectOutput)
        return DiagnosticSentinelInput(
            root=ctx.require_workspace(),
            repo=ctx.require(StageName.REPO_SCOUT, RepoScoutOutput),
            sandbox=ctx.optional(StageName.SANDBOX_RUNNER, SandboxRunnerOutput),
            graph=architect.graph if architect else None,
        )

    async def run(self, inp: DiagnosticSentinelInput, log: StageLogger) -> DiagnosticSentinelOutput:
        out = await asyncio.to_thread(lambda: diagnose(inp, open_memory(log)))
        seen = [f for f in out.findings if f.seen_before]
        if seen:
            raised = sum(f.base_severity is not None for f in seen)
            log.info(f"{len(seen)} findings match patterns seen in earlier runs; severity raised for {raised}")
        for report in out.sources:
            line = f"{report.source}: {report.status}, {report.findings} findings" + (
                f" ({report.detail})" if report.detail else ""
            )
            (log.warning if report.status is not SourceStatus.OK else log.info)(line)
        by_severity = {s: sum(f.severity is s for f in out.findings) for s in SEVERITY_ORDER}
        counts = ", ".join(f"{n} {s}" for s, n in by_severity.items() if n)
        log.info(f"{len(out.findings)} findings" + (f": {counts}" if counts else ""))
        if not any(r.status is SourceStatus.OK for r in out.sources):
            raise RuntimeError("no diagnostic source could run: " + "; ".join(f"{r.source}: {r.detail}" for r in out.sources))
        return out


def diagnose(inp: DiagnosticSentinelInput, memory: MemoryStore | None = None) -> DiagnosticSentinelOutput:
    """Collect and rate findings. With a ``memory``, each finding is also signed, and a pattern recorded in
    an earlier run gets its severity raised one level and is marked ``seen_before``."""
    index = NodeIndex(inp.graph)
    findings: list[Finding] = []
    reports: list[SourceReport] = []

    def collect(source: FindingSource, fn: Callable[[], list[Finding]]) -> None:
        try:
            found = fn()
        except Exception as exc:  # one collector failing must not hide what the others found
            reports.append(SourceReport(source=source, status=SourceStatus.FAILED, detail=f"{type(exc).__name__}: {exc}"))
            return
        findings.extend(found)
        reports.append(SourceReport(source=source, status=SourceStatus.OK, findings=len(found)))

    if inp.sandbox is None:
        reports.append(
            SourceReport(source=FindingSource.SANDBOX, status=SourceStatus.SKIPPED, detail="SandboxRunner produced no results")
        )
    else:
        collect(FindingSource.SANDBOX, lambda: _sandbox_findings(inp, index))
    collect(FindingSource.BANDIT, lambda: _bandit_findings(inp, index))
    collect(FindingSource.STATIC, lambda: _race_findings(inp, index))

    # The same rule at the same place is one finding, whichever way it was reached.
    unique = list({f.id: f for f in findings}.values())
    if memory is not None:
        unique = remember(unique, inp, memory)
    ordered = sorted(unique, key=lambda f: (SEVERITY_ORDER[f.severity], f.location.file, f.location.line, f.rule_id))
    return DiagnosticSentinelOutput(findings=ordered, sources=reports)


def remember(findings: list[Finding], inp: DiagnosticSentinelInput, memory: MemoryStore) -> list[Finding]:
    """Sign the findings and raise the severity of patterns the memory has seen in earlier runs."""
    test_files = {m.path for m in inp.repo.modules if m.is_test}
    signed = attach_signatures(findings, inp.root, test_files)
    seen = memory.seen_counts(f.signature for f in signed if f.signature)
    result = []
    for f in signed:
        runs = seen.get(f.signature or "", 0)
        if not runs:
            result.append(f)
            continue
        higher = boosted(f.severity)
        result.append(
            f.model_copy(update={"seen_before": runs, "severity": higher, "base_severity": f.severity if higher != f.severity else None})
        )
    return result


def finding_id(rule_id: str, location: SourceLocation) -> str:
    digest = hashlib.sha1(f"{rule_id}:{location.file}:{location.line}".encode()).hexdigest()
    return f"F-{digest[:8]}"


def _node_id(index: NodeIndex, location: SourceLocation) -> str | None:
    node = index.at(location.file, location.line)
    return node.id if node else None


def _sandbox_findings(inp: DiagnosticSentinelInput, index: NodeIndex) -> list[Finding]:
    assert inp.sandbox is not None
    findings = []
    for failure in collect_failures(inp.sandbox, inp.repo):
        rule_id = f"EXC-{failure.exception_type}"
        also = f" (and {failure.count - 1} more: {', '.join(failure.other_origins[:3])})" if failure.count > 1 else ""
        owasp = SQL_SYNTAX_OWASP if failure.sql_syntax else None
        title = (
            "Query fails on quoting characters: input reaches SQL unescaped"
            if failure.sql_syntax
            else f"{failure.exception_type} raised"
            + (f" in {failure.location.file}" if failure.in_app_code else " in a test")
        )
        findings.append(
            Finding(
                id=finding_id(rule_id, failure.location),
                category=FindingCategory.EXCEPTION,
                source=FindingSource.SANDBOX,
                rule_id=rule_id,
                title=title,
                severity=failure.severity,
                location=failure.location,
                node_id=_node_id(index, failure.location),
                evidence=f"{failure.exception_type}: {failure.message}{also}".strip(),
                owasp=owasp,
                owasp_name=owasp_name(owasp),
                exception=ExceptionInfo(type=failure.exception_type, message=failure.message, frames=failure.frames),
                source_test=failure.origin,
            )
        )
    return findings


def _bandit_findings(inp: DiagnosticSentinelInput, index: NodeIndex) -> list[Finding]:
    test_files = {m.path for m in inp.repo.modules if m.is_test}
    findings = []
    for issue in run_bandit(inp.root, DEFAULT_EXCLUDE_DIRS):
        if issue.file in test_files or issue.owasp is None:
            continue  # tests are not shipped; unmapped results are code-quality notes, not weaknesses
        location = SourceLocation(file=issue.file, line=issue.line, end_line=issue.end_line)
        cwe = f", CWE-{issue.cwe}" if issue.cwe else ""
        findings.append(
            Finding(
                id=finding_id(issue.test_id, location),
                category=FindingCategory.SECURITY,
                source=FindingSource.BANDIT,
                rule_id=issue.test_id,
                title=issue.text.rstrip("."),
                severity=issue.severity,
                location=location,
                node_id=_node_id(index, location),
                evidence=(
                    f"bandit {issue.test_id} ({issue.test_name}): {issue.text} "
                    f"[severity {issue.bandit_severity}, confidence {issue.confidence}{cwe}]"
                ),
                owasp=issue.owasp,
                owasp_name=owasp_name(issue.owasp),
            )
        )
    return findings


def _race_findings(inp: DiagnosticSentinelInput, index: NodeIndex) -> list[Finding]:
    findings = []
    for issue in find_races(inp.root, inp.repo):
        location = SourceLocation(file=issue.file, line=issue.line)
        findings.append(
            Finding(
                id=finding_id(issue.rule_id, location),
                category=FindingCategory.RACE_CONDITION,
                source=FindingSource.STATIC,
                rule_id=issue.rule_id,
                title=issue.title,
                severity=issue.severity,
                location=location,
                node_id=_node_id(index, location),
                evidence=describe(issue),
            )
        )
    return findings

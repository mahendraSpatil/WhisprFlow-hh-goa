from __future__ import annotations

import asyncio
from collections import defaultdict
from dataclasses import dataclass, field

from app.agents.base import Agent
from app.agents.diagnostic_sentinel import diagnose
from app.agents.indexer import index_repository
from app.agents.patching import apply_patch
from app.models.agents import (
    DEFAULT_EXCLUDE_DIRS,
    CaseOutcome,
    DiagnosticSentinelInput,
    DiagnosticSentinelOutput,
    Finding,
    FindingRef,
    FindingSource,
    Patch,
    PatchMasterOutput,
    PatchStatus,
    PatchVerdict,
    RegressionCheckInput,
    RegressionCheckOutput,
    RegressionSummary,
    RepoScoutOutput,
    SandboxRunnerOutput,
    SourceKind,
    SuiteTotals,
    Symbol,
    SymbolKind,
    SystemAnalystOutput,
    Verdict,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger
from app.sandbox import Sandbox, ensure_environment, fresh_copy, remove_tree, run_suite

BAD = {CaseOutcome.FAILED, CaseOutcome.ERROR}
MAX_LISTED = 3


class RegressionCheck(Agent[RegressionCheckInput, RegressionCheckOutput]):
    """Applies the valid patches to fresh copies of the repo and re-runs the tests and the scans.

    Each patch is first evaluated alone, so what it fixes and what it breaks are attributed exactly:
    a test that passed before and fails now, or a finding that was not there before, rejects it, with
    the reason. The surviving patches are then applied together to confirm they do not interfere
    (a patch that conflicts with an earlier one, or breaks only in combination, is rejected too).
    "Before" is the SandboxRunner and DiagnosticSentinel output; "after" is produced by the same code.
    """

    name = StageName.REGRESSION_CHECK
    title = "RegressionCheck"
    output_model = RegressionCheckOutput
    requires = (StageName.SYSTEM_ANALYST, StageName.PATCH_MASTER)
    uses = (StageName.SANDBOX_RUNNER, StageName.DIAGNOSTIC_SENTINEL)
    default_timeout_s = 1800.0

    def build_input(self, ctx: RunContext) -> RegressionCheckInput:
        system = ctx.require(StageName.SYSTEM_ANALYST, SystemAnalystOutput)
        sentinel = ctx.optional(StageName.DIAGNOSTIC_SENTINEL, DiagnosticSentinelOutput)
        patches = ctx.require(StageName.PATCH_MASTER, PatchMasterOutput).patches
        return RegressionCheckInput(
            root=ctx.require_workspace(),
            work_dir=ctx.work_dir,
            repo=ctx.require(StageName.REPO_SCOUT, RepoScoutOutput),
            patches=[p for p in patches if p.status is PatchStatus.VALID],
            findings=sentinel.findings if sentinel else None,
            dependencies=system.dependencies,
            test_runner=system.test_runner,
            test_paths=system.test_paths,
            baseline=ctx.optional(StageName.SANDBOX_RUNNER, SandboxRunnerOutput),
        )

    async def run(self, inp: RegressionCheckInput, log: StageLogger) -> RegressionCheckOutput:
        if not inp.patches:
            log.info("No valid patches to verify")
            return RegressionCheckOutput(baseline_totals=inp.baseline.totals if inp.baseline else None)
        out = await asyncio.to_thread(check, inp, log)
        for v in out.verdicts:
            log.info(f"{v.patch_id}: {'accepted' if v.accepted else 'REJECTED'} ({v.verdict})" + "".join(f"; {r}" for r in v.reasons))
        if s := out.summary:
            log.info(
                f"Together: {s.tests_before.passed} -> {s.tests_after.passed} tests passing "
                f"({len(s.tests_fixed)} fixed, {len(s.tests_broken)} broken); findings {s.findings_before} -> {s.findings_after}"
            )
        return out


# --- Evaluating the repo with some patches applied --------------------------------------------


@dataclass
class Evaluation:
    applied: list[str] = field(default_factory=list)
    redundant: list[str] = field(default_factory=list)
    failed: dict[str, str] = field(default_factory=dict)
    tests: SandboxRunnerOutput | None = None
    findings: list[Finding] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)


class Checker:
    def __init__(self, inp: RegressionCheckInput, log: StageLogger) -> None:
        self.inp, self.log = inp, log
        self.sandbox: Sandbox | None = ensure_environment(inp.work_dir, inp.dependencies, log) if inp.test_runner else None
        self._runs = 0

    def evaluate(self, label: str, patches: list[Patch]) -> Evaluation:
        """A fresh copy of the repo with ``patches`` applied in order, then the tests and the scans."""
        self._runs += 1
        scratch = self.inp.work_dir / "regression" / f"{self._runs:02d}-{label}"
        copy = fresh_copy(self.inp.root, scratch / "repo")
        try:
            ev = Evaluation()
            for patch in patches:
                status, message = apply_patch(copy, patch.diff, [f.path for f in patch.files])
                if status == "applied":
                    ev.applied.append(patch.id)
                elif status == "redundant":
                    ev.redundant.append(patch.id)
                else:
                    ev.failed[patch.id] = message
            if self.sandbox is not None:
                ev.tests = run_suite(self.sandbox, copy, self.inp.test_paths, self.log)
            index = index_repository(copy, set(DEFAULT_EXCLUDE_DIRS), 1_000_000)
            after = RepoScoutOutput(
                root=copy, source_kind=SourceKind.LOCAL, modules=index.modules, symbols=index.symbols, calls=index.calls
            )
            ev.findings = diagnose(DiagnosticSentinelInput(root=copy, repo=after, sandbox=ev.tests, graph=None)).findings
            ev.symbols = after.symbols
            t = ev.tests.totals if ev.tests else None
            self.log.info(
                f"{label}: {len(ev.applied)} applied"
                + (f", {len(ev.failed)} did not apply" if ev.failed else "")
                + (f"; tests {t.passed} passed, {t.failed + t.errors} failing" if t else "")
                + f"; {len(ev.findings)} findings"
            )
            return ev
        finally:
            remove_tree(scratch)


# --- Comparing -----------------------------------------------------------------------------------


def compare_tests(before: SandboxRunnerOutput | None, after: SandboxRunnerOutput | None) -> tuple[list[str], list[str]]:
    """(fixed, broken): failing tests that now pass, and passing (or new) tests that now fail or vanished."""
    if before is None or after is None:
        return [], []
    was = {t.node_id: t.outcome for t in before.tests}
    now = {t.node_id: t.outcome for t in after.tests}
    fixed = [n for n, o in was.items() if o in BAD and now.get(n) is CaseOutcome.PASSED]
    broken = [n for n, o in was.items() if o is CaseOutcome.PASSED and (now.get(n) in BAD or n not in now)]
    broken += [n for n, o in now.items() if o in BAD and n not in was]  # e.g. a collection error or a timeout
    return fixed, broken


def _key_function(symbols: list[Symbol]):
    """Identify a finding by rule, file and enclosing function, so edits that shift line numbers keep its identity."""
    by_file: dict[str, list[Symbol]] = defaultdict(list)
    for s in symbols:
        if s.kind is not SymbolKind.CLASS:
            by_file[s.location.file].append(s)

    def span(s: Symbol) -> int:
        return (s.location.end_line or s.location.line) - s.location.line

    def key(f: Finding) -> tuple[str, str, str]:
        inside = [
            s for s in by_file.get(f.location.file, ())
            if s.location.line <= f.location.line <= (s.location.end_line or s.location.line)
        ]
        best = min(inside, key=span, default=None)
        return f.rule_id, f.location.file, best.qualname if best else "<module>"

    return key


def compare_findings(
    before: list[Finding], before_symbols: list[Symbol], after: list[Finding], after_symbols: list[Symbol]
) -> tuple[list[Finding], list[Finding]]:
    """(resolved, introduced): findings gone since before, and findings that were not there."""
    key_before, key_after = _key_function(before_symbols), _key_function(after_symbols)
    was: dict[tuple, list[Finding]] = defaultdict(list)
    now: dict[tuple, list[Finding]] = defaultdict(list)
    for f in before:
        was[key_before(f)].append(f)
    for f in after:
        now[key_after(f)].append(f)
    resolved: list[Finding] = []
    introduced: list[Finding] = []
    for key in was.keys() | now.keys():
        had, has = len(was[key]), len(now[key])
        if had > has:
            resolved += was[key][: had - has]
        elif has > had:
            introduced += now[key][: has - had]

    def order(f: Finding) -> tuple[str, int, str]:
        return f.location.file, f.location.line, f.rule_id

    return sorted(resolved, key=order), sorted(introduced, key=order)


def ref(f: Finding) -> FindingRef:
    return FindingRef(
        finding_id=f.id, rule_id=f.rule_id, title=f.title, severity=f.severity, location=f.location, node_id=f.node_id
    )


def _first(message: str) -> str:
    return (message.strip().splitlines() or ["no detail from git"])[0]


def _listed(items: list[str]) -> str:
    shown = ", ".join(items[:MAX_LISTED])
    return shown + (f" (+{len(items) - MAX_LISTED} more)" if len(items) > MAX_LISTED else "")


def problems(broken: list[str], introduced: list[Finding]) -> list[str]:
    reasons = []
    if broken:
        reasons.append(f"breaks {len(broken)} test{'s' if len(broken) != 1 else ''} that passed before: {_listed(broken)}")
    if introduced:
        what = [f"{f.rule_id} at {f.location.file}:{f.location.line}" for f in introduced]
        reasons.append(f"introduces {len(introduced)} new finding{'s' if len(introduced) != 1 else ''}: {_listed(what)}")
    return reasons


# --- The check ------------------------------------------------------------------------------------


def check(inp: RegressionCheckInput, log: StageLogger) -> RegressionCheckOutput:
    checker = Checker(inp, log)

    baseline, before = inp.baseline, inp.findings
    if (baseline is None and inp.test_runner) or before is None:
        ev0 = checker.evaluate("baseline", [])  # the sandbox or the scan did not run in the pipeline: measure it here
        baseline = baseline or ev0.tests
        before = before if before is not None else ev0.findings
    symbols = inp.repo.symbols

    def against_baseline(ev: Evaluation):
        fixed, broken = compare_tests(baseline, ev.tests)
        resolved, introduced = compare_findings(before, symbols, ev.findings, ev.symbols)
        # A sandbox finding for a test that newly fails only restates that broken test; do not count it twice.
        broke = set(broken)
        introduced = [f for f in introduced if not (f.source is FindingSource.SANDBOX and f.source_test in broke)]
        return fixed, broken, resolved, introduced

    verdicts: dict[str, PatchVerdict] = {}
    for patch in inp.patches:
        ev = checker.evaluate(f"alone-{patch.id}", [patch])
        verdicts[patch.id] = judge(patch, ev, *against_baseline(ev))

    final: Evaluation | None = None
    accepted = [p for p in inp.patches if verdicts[p.id].accepted]
    if accepted:
        combined = checker.evaluate("together", accepted)
        for pid, error in combined.failed.items():
            reject(verdicts[pid], Verdict.APPLY_FAILED, [f"does not apply on top of the other accepted patches ({_first(error)})"])
        for pid in combined.redundant:
            verdicts[pid].reasons.append("its change is already made by an earlier accepted patch, so it was not applied again")
        _, broken, _, introduced = against_baseline(combined)
        if not (broken or introduced):
            final = combined
        else:  # each patch was fine alone, so they interfere (or a test is flaky): rebuild one patch at a time
            survivors = [p for p in accepted if verdicts[p.id].accepted]
            final = isolate_interference(checker, survivors, verdicts, against_baseline)

    return RegressionCheckOutput(
        baseline_totals=baseline.totals if baseline else None,
        verdicts=[verdicts[p.id] for p in inp.patches],
        summary=summarize(baseline, before, final, against_baseline),
    )


def judge(
    patch: Patch, ev: Evaluation, fixed: list[str], broken: list[str], resolved: list[Finding], introduced: list[Finding]
) -> PatchVerdict:
    verdict = PatchVerdict(
        patch_id=patch.id,
        root_cause_id=patch.root_cause_id,
        verdict=Verdict.PASS,
        accepted=True,
        totals=ev.tests.totals if ev.tests else SuiteTotals(),
        newly_passing=fixed,
        newly_failing=broken,
        findings_resolved=[ref(f) for f in resolved],
        findings_introduced=[ref(f) for f in introduced],
    )
    if patch.id in ev.failed:
        reject(verdict, Verdict.APPLY_FAILED, [f"does not apply to a fresh copy of the repo ({_first(ev.failed[patch.id])})"])
        verdict.detail = ev.failed[patch.id]
    elif broken or introduced:
        reject(verdict, Verdict.REGRESSED, problems(broken, introduced))
    elif not fixed and not resolved:
        verdict.reasons.append("applies cleanly, but no failing test and no finding changed")
    return verdict


def reject(verdict: PatchVerdict, kind: Verdict, reasons: list[str]) -> None:
    verdict.verdict, verdict.accepted = kind, False
    verdict.reasons = list(reasons)  # replaces any notes written while the patch was still accepted


def isolate_interference(checker: Checker, candidates: list[Patch], verdicts: dict[str, PatchVerdict], against_baseline) -> Evaluation | None:
    """Add the accepted patches one at a time; one that breaks the build so far is rejected."""
    kept: list[Patch] = []
    last: Evaluation | None = None
    for patch in candidates:
        trial = checker.evaluate(f"build-{len(kept) + 1}", [*kept, patch])
        _, broken, _, introduced = against_baseline(trial)
        others = _listed([p.id for p in kept])
        if patch.id in trial.failed:
            reject(verdicts[patch.id], Verdict.APPLY_FAILED, [f"does not apply on top of {others or 'the earlier patches'}"])
        elif broken or introduced:
            context = f"breaks only in combination with {others}" if kept else "breaks when it is checked again (the result is not stable)"
            reject(verdicts[patch.id], Verdict.REGRESSED, [f"{context}: " + "; ".join(problems(broken, introduced))])
        else:
            kept.append(patch)
            last = trial
    return last


def summarize(baseline: SandboxRunnerOutput | None, before: list[Finding], final: Evaluation | None, against_baseline) -> RegressionSummary:
    zero = SuiteTotals()
    summary = RegressionSummary(
        tests_before=baseline.totals if baseline else zero,
        tests_after=baseline.totals if baseline else zero,
        findings_before=len(before),
        findings_after=len(before),
    )
    if final is None or not final.applied:
        return summary  # nothing was accepted: after equals before
    fixed, broken, resolved, introduced = against_baseline(final)
    summary.patches_applied = final.applied
    summary.tests_after = final.tests.totals if final.tests else zero
    summary.tests_fixed, summary.tests_broken = fixed, broken
    summary.findings_after = len(final.findings)
    summary.findings_resolved = [ref(f) for f in resolved]
    summary.findings_introduced = [ref(f) for f in introduced]
    return summary

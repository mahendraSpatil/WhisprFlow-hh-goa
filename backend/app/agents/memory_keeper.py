from __future__ import annotations

import asyncio

from app.agents.base import Agent
from app.memory import MemoryStore, NewIncident, kind_of, memory_enabled
from app.models.agents import (
    DiagnosticSentinelOutput,
    IncidentRecord,
    MemoryKeeperInput,
    MemoryKeeperOutput,
    PatchMasterOutput,
    RegressionCheckOutput,
    RootCauseDiagnosticianOutput,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger


class MemoryKeeper(Agent[MemoryKeeperInput, MemoryKeeperOutput]):
    """Records each incident of the run in the SQLite memory, and whether its fix held up.

    An incident is a finding with its signature (exception type or rule, normalized code pattern,
    OWASP category), the root cause, the patch and the regression verdict. Later runs read this: a
    pattern seen before gets its severity raised, and accepted fixes become examples for PatchMaster.
    Findings in test code carry no signature and are not remembered. Only findings are required, so
    incidents are recorded even when diagnosis, patching or verification escalated.
    """

    name = StageName.MEMORY_KEEPER
    title = "MemoryKeeper"
    output_model = MemoryKeeperOutput
    requires = (StageName.DIAGNOSTIC_SENTINEL,)
    uses = (StageName.ROOT_CAUSE_DIAGNOSTICIAN, StageName.PATCH_MASTER, StageName.REGRESSION_CHECK)
    default_timeout_s = 60.0

    def build_input(self, ctx: RunContext) -> MemoryKeeperInput:
        root_causes = ctx.optional(StageName.ROOT_CAUSE_DIAGNOSTICIAN, RootCauseDiagnosticianOutput)
        patches = ctx.optional(StageName.PATCH_MASTER, PatchMasterOutput)
        regression = ctx.optional(StageName.REGRESSION_CHECK, RegressionCheckOutput)
        return MemoryKeeperInput(
            run_id=ctx.run_id,
            source=ctx.source,
            findings=ctx.require(StageName.DIAGNOSTIC_SENTINEL, DiagnosticSentinelOutput).findings,
            root_causes=root_causes.root_causes if root_causes else [],
            patches=patches.patches if patches else [],
            verdicts=regression.verdicts if regression else [],
        )

    async def run(self, inp: MemoryKeeperInput, log: StageLogger) -> MemoryKeeperOutput:
        if not memory_enabled():
            log.info("CODELOOP_MEMORY=off: nothing is remembered")
            return MemoryKeeperOutput()
        # A store that cannot be written is this stage's whole job failing, so errors propagate (and retry).
        out = await asyncio.to_thread(remember_incidents, inp, MemoryStore.default())
        new = sum(r.is_new for r in out.incidents)
        log.info(f"Remembered {len(out.incidents)} incidents ({new} new patterns, {len(out.incidents) - new} seen before)")
        return out


def remember_incidents(inp: MemoryKeeperInput, store: MemoryStore | None) -> MemoryKeeperOutput:
    if store is None:
        return MemoryKeeperOutput()
    cause_of = {finding_id: cause for cause in inp.root_causes for finding_id in cause.finding_ids}
    patch_of = {p.root_cause_id: p for p in inp.patches}
    verdict_of = {v.patch_id: v for v in inp.verdicts}

    records = []
    for finding in inp.findings:
        if not finding.signature or not finding.pattern:
            continue  # test code, or nothing to normalize
        cause = cause_of.get(finding.id)
        patch = patch_of.get(cause.id) if cause else None
        verdict = verdict_of.get(patch.id) if patch else None
        seen_in_earlier_runs = store.seen_counts([finding.signature], exclude_run=inp.run_id).get(finding.signature, 0)
        store.record(
            NewIncident(
                signature=finding.signature,
                run_id=inp.run_id,
                finding_id=finding.id,
                source=inp.source,
                kind=kind_of(finding),
                pattern=finding.pattern,
                owasp=finding.owasp,
                category=finding.category.value,
                severity=finding.severity.value,
                rule_id=finding.rule_id,
                title=finding.title,
                file=finding.location.file,
                line=finding.location.line,
                symbol=cause.symbol if cause else None,
                root_cause=cause.explanation if cause else None,
                patch_status=patch.status.value if patch else "none",
                patch_diff=patch.diff or None if patch else None,
                patch_design=patch.design_suggestion or None if patch else None,
                regression_passed=verdict.accepted if verdict else None,
                regression_reasons=tuple(verdict.reasons) if verdict else (),
            )
        )
        records.append(
            IncidentRecord(
                signature=finding.signature,
                finding_id=finding.id,
                root_cause_id=cause.id if cause else None,
                patch_id=patch.id if patch else None,
                regression_passed=verdict.accepted if verdict else None,
                is_new=seen_in_earlier_runs == 0,
                occurrences=store.occurrences(finding.signature),
            )
        )
    return MemoryKeeperOutput(incidents=records)

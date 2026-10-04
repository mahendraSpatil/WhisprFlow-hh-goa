from __future__ import annotations

import asyncio

from app.agents.base import Agent
from app.models.agents import (
    CaseOutcome,
    Isolation,
    PipelineArchitectOutput,
    SandboxRunnerInput,
    SandboxRunnerOutput,
    SystemAnalystOutput,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger
from app.sandbox import ensure_environment, fresh_copy, run_suite

MAX_FAILURES_LOGGED = 5


class SandboxRunner(Agent[SandboxRunnerInput, SandboxRunnerOutput]):
    """Runs the repo's tests in a sandbox: a fresh virtualenv, a private copy, a timeout, no network.

    See ``app.sandbox`` for exactly what the isolation does and does not guarantee. Synthetic-input
    probing of entry functions is not implemented yet; only the repo's own test suite runs.
    """

    name = StageName.SANDBOX_RUNNER
    title = "SandboxRunner"
    output_model = SandboxRunnerOutput
    requires = (StageName.SYSTEM_ANALYST,)
    uses = (StageName.PIPELINE_ARCHITECT,)
    default_timeout_s = 900.0  # a fresh virtualenv and pip install come before the tests

    def build_input(self, ctx: RunContext) -> SandboxRunnerInput:
        system = ctx.require(StageName.SYSTEM_ANALYST, SystemAnalystOutput)
        architect = ctx.optional(StageName.PIPELINE_ARCHITECT, PipelineArchitectOutput)
        return SandboxRunnerInput(
            root=ctx.require_workspace(),
            work_dir=ctx.work_dir,
            dependencies=system.dependencies,
            test_runner=system.test_runner,
            test_paths=system.test_paths,
            entrypoints=system.entrypoints,
            graph=architect.graph if architect else None,
        )

    async def run(self, inp: SandboxRunnerInput, log: StageLogger) -> SandboxRunnerOutput:
        if inp.test_runner is None:
            log.info("No tests were found, so there is nothing to run")
            return SandboxRunnerOutput(isolation=Isolation.SUBPROCESS)
        out = await asyncio.to_thread(self._run, inp, log)
        t = out.totals
        log.info(f"{t.passed} passed, {t.failed} failed, {t.errors} errors, {t.skipped} skipped in {out.duration_s}s")
        failing = [c for c in out.tests if c.outcome in (CaseOutcome.FAILED, CaseOutcome.ERROR)]
        for case in failing[:MAX_FAILURES_LOGGED]:
            message = (case.message or "").strip()
            first_line = message.splitlines()[0] if message else "failed"  # pytest messages can run to many lines
            log.warning(f"{case.node_id}: {first_line[:240]}")
        if len(failing) > MAX_FAILURES_LOGGED:
            log.warning(f"... and {len(failing) - MAX_FAILURES_LOGGED} more failing tests")
        return out

    @staticmethod
    def _run(inp: SandboxRunnerInput, log: StageLogger) -> SandboxRunnerOutput:
        sandbox = ensure_environment(inp.work_dir, inp.dependencies, log)
        repo = fresh_copy(inp.root, inp.work_dir / "sandbox" / "baseline" / "repo")
        log.info(f"Running {inp.test_runner} in a private copy of the repo ({sandbox.isolation} isolation)")
        return run_suite(sandbox, repo, inp.test_paths, log)

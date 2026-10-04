from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agents import default_pipeline
from app.agents.base import Agent
from app.agents.pipeline_architect import PipelineArchitect
from app.agents.root_cause_diagnostician import RootCauseDiagnostician
from app.models.agents import ArchitectureStyle, EntrypointKind, Layer
from app.models.events import LogEvent, RunStatusEvent, StageStatusEvent
from app.models.run import RunStatus, SourceKind, StageName, StageStatus
from app.orchestrator.runner import Orchestrator, PipelineConfigError

pytestmark = pytest.mark.anyio


class FlakyRootCause(RootCauseDiagnostician):
    """Fails the first ``failures`` attempts, then behaves normally."""

    def __init__(self, failures: int, timeout_s: float | None = None) -> None:
        super().__init__(timeout_s=timeout_s)
        self.failures = failures
        self.calls = 0

    async def run(self, inp, log):
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError(f"boom {self.calls}")
        return await super().run(inp, log)


class HangingArchitect(PipelineArchitect):
    async def run(self, inp, log):
        await asyncio.sleep(10)
        raise AssertionError("unreachable")


def pipeline_with(*replacements: Agent) -> list[Agent]:
    by_name = {a.name: a for a in replacements}
    return [by_name.get(a.name, a) for a in default_pipeline()]


async def execute(tmp_path: Path, source: Path, agents: list[Agent] | None = None):
    orchestrator = Orchestrator(workspace_root=tmp_path / "workspaces", agents=agents)
    ctx, events = orchestrator.create_run(SourceKind.LOCAL, str(source))
    await orchestrator.execute(ctx, events)
    return ctx, events.history


def stage_transitions(history, stage: StageName) -> list[StageStatus]:
    return [e.status for e in history if isinstance(e, StageStatusEvent) and e.stage == stage]


async def test_full_pipeline_succeeds_and_streams_in_order(tmp_path, sample_repo):
    ctx, history = await execute(tmp_path, sample_repo)

    assert ctx.status is RunStatus.COMPLETED
    assert all(r.status is StageStatus.SUCCESS and r.attempts == 1 for r in ctx.stages.values())
    assert [e.seq for e in history] == list(range(1, len(history) + 1))

    run_statuses = [e.status for e in history if isinstance(e, RunStatusEvent)]
    assert run_statuses == [RunStatus.QUEUED, RunStatus.RUNNING, RunStatus.COMPLETED]

    started = [e.stage for e in history if isinstance(e, StageStatusEvent) and e.status is StageStatus.RUNNING]
    assert started == list(StageName)
    assert any(isinstance(e, LogEvent) and e.stage is StageName.REPO_SCOUT for e in history)

    # The workspace is a copy inside the run's work dir; the source repo is untouched.
    workspace = ctx.require_workspace()
    assert workspace == ctx.work_dir / "repo" and workspace != sample_repo
    assert (workspace / "sample" / "api.py").is_file()


async def test_real_agents_index_the_sample_repo(tmp_path, sample_repo):
    ctx, _ = await execute(tmp_path, sample_repo)

    repo = ctx.repo_scout
    assert {m.name for m in repo.modules} >= {"sample", "sample.api", "sample.service", "tests.test_service"}
    assert [e.path for e in repo.parse_errors] == ["sample/broken.py"]
    api_imports = next(m for m in repo.modules if m.name == "sample.api").imports
    assert any(i.module == "sample.service" and i.level == 1 for i in api_imports)

    system = ctx.system_analyst
    assert system.architecture is ArchitectureStyle.WEB_SERVICE
    assert system.test_runner == "pytest"
    fastapi = next(c for c in system.stack if c.key == "fastapi")
    assert fastapi.declared and fastapi.used_in_app
    assert system.layer_of("sample.api") is Layer.API
    kinds = {(e.kind, e.symbol or e.module) for e in system.entrypoints}
    assert (EntrypointKind.WEB_ROUTE, "sample.api.get_total") in kinds
    assert (EntrypointKind.MAIN_GUARD, "sample.service") in kinds

    graph = ctx.pipeline_architect.graph
    edges = {(e.kind.value, e.source, e.target) for e in graph.edges}
    assert ("call", "fn:sample.api.get_total", "fn:sample.service.total") in edges
    assert ("call", "main:sample.service", "fn:sample.service.total") in edges
    types = {n.id: n.type.value for n in graph.nodes}
    assert types["fn:sample.api.get_total"] == "entry"  # a FastAPI route


async def test_one_failure_is_retried_and_succeeds(tmp_path, sample_repo):
    agent = FlakyRootCause(failures=1)
    ctx, history = await execute(tmp_path, sample_repo, pipeline_with(agent))

    record = ctx.stages[StageName.ROOT_CAUSE_DIAGNOSTICIAN]
    assert record.status is StageStatus.SUCCESS
    assert record.attempts == 2 and agent.calls == 2
    assert [e.message for e in record.errors] == ["boom 1"]
    assert ctx.errors == []
    assert ctx.status is RunStatus.COMPLETED
    assert stage_transitions(history, StageName.ROOT_CAUSE_DIAGNOSTICIAN) == [
        StageStatus.PENDING, StageStatus.RUNNING, StageStatus.FAILED, StageStatus.RUNNING, StageStatus.SUCCESS,
    ]


async def test_escalation_skips_only_dependent_stages(tmp_path, sample_repo):
    ctx, history = await execute(tmp_path, sample_repo, pipeline_with(FlakyRootCause(failures=2)))

    statuses = {name: r.status for name, r in ctx.stages.items()}
    assert statuses[StageName.ROOT_CAUSE_DIAGNOSTICIAN] is StageStatus.ESCALATED
    # Hard dependents are skipped, transitively.
    assert statuses[StageName.PATCH_MASTER] is StageStatus.SKIPPED
    assert statuses[StageName.REGRESSION_CHECK] is StageStatus.SKIPPED
    # MemoryKeeper only *uses* the root causes, so it still runs.
    assert statuses[StageName.MEMORY_KEEPER] is StageStatus.SUCCESS
    assert ctx.memory_keeper is not None and ctx.patch_master is None

    assert ctx.status is RunStatus.COMPLETED_WITH_ESCALATIONS
    assert [(e.stage, e.attempt, e.message) for e in ctx.errors] == [
        (StageName.ROOT_CAUSE_DIAGNOSTICIAN, 2, "boom 2")
    ]
    assert ctx.stages[StageName.PATCH_MASTER].skipped_reason.endswith("RootCauseDiagnostician")
    assert ctx.stages[StageName.REGRESSION_CHECK].skipped_reason.endswith("PatchMaster")

    escalated = [e for e in history if isinstance(e, StageStatusEvent) and e.status is StageStatus.ESCALATED]
    assert len(escalated) == 1 and escalated[0].error.traceback


async def test_timeout_escalates_and_soft_dependents_still_run(tmp_path, sample_repo):
    ctx, _ = await execute(tmp_path, sample_repo, pipeline_with(HangingArchitect(timeout_s=0.05)))

    record = ctx.stages[StageName.PIPELINE_ARCHITECT]
    assert record.status is StageStatus.ESCALATED
    assert record.attempts == 2
    assert all(e.timed_out for e in record.errors)
    # Nothing *requires* the graph, so every other stage succeeds without it.
    others = [r for n, r in ctx.stages.items() if n is not StageName.PIPELINE_ARCHITECT]
    assert all(r.status is StageStatus.SUCCESS for r in others)
    assert ctx.status is RunStatus.COMPLETED_WITH_ESCALATIONS


async def test_fetch_failure_escalates_repo_scout_and_fails_run(tmp_path):
    missing = tmp_path / "does-not-exist"
    ctx, history = await execute(tmp_path, missing)

    scout = ctx.stages[StageName.REPO_SCOUT]
    assert scout.status is StageStatus.ESCALATED and scout.attempts == 2
    assert all(r.status is StageStatus.SKIPPED for n, r in ctx.stages.items() if n is not StageName.REPO_SCOUT)
    assert [e.stage for e in ctx.errors] == [StageName.REPO_SCOUT]
    assert ctx.status is RunStatus.FAILED  # nothing succeeded
    assert isinstance(history[-1], RunStatusEvent) and history[-1].status is RunStatus.FAILED


async def test_subscriber_replays_history_and_follows_live(tmp_path, sample_repo):
    orchestrator = Orchestrator(workspace_root=tmp_path / "ws")
    ctx, events = orchestrator.create_run(SourceKind.LOCAL, str(sample_repo))

    async def collect(since: int):
        return [e async for e in events.subscribe(since)]

    early = asyncio.create_task(collect(0))
    resumed = asyncio.create_task(collect(5))
    await orchestrator.execute(ctx, events)

    full = await early
    assert [e.seq for e in full] == list(range(1, len(events.history) + 1))
    assert [e.seq for e in await resumed] == list(range(6, len(events.history) + 1))


def test_pipeline_validation_rejects_out_of_order_dependencies(tmp_path):
    agents = default_pipeline()
    agents[0], agents[1] = agents[1], agents[0]
    with pytest.raises(PipelineConfigError, match="does not run before it"):
        Orchestrator(workspace_root=tmp_path, agents=agents)

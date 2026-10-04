"""Runs the agent pipeline for one RunContext, streaming every state change."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections.abc import Sequence
from pathlib import Path

from app.agents import Agent, default_pipeline
from app.models.agents import SourceKind
from app.models.events import RunStatusEvent, StageStatusEvent
from app.models.run import (
    RunContext,
    RunStatus,
    StageError,
    StageName,
    StageRecord,
    StageStatus,
    utcnow,
)
from app.orchestrator.events import RunEventStream, StageLogger
from app.sources import redact

logger = logging.getLogger("codeloop")


class PipelineConfigError(ValueError):
    pass


class Orchestrator:
    """Executes agents strictly in order.

    Each stage gets ``max_attempts`` tries (default: the first run plus one
    retry), each bounded by the agent's timeout. A stage whose attempts are all
    used up is escalated; later stages that *require* it are skipped, while
    stages that only *use* it still run without its output.
    """

    def __init__(self, workspace_root: Path, agents: Sequence[Agent] | None = None, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise PipelineConfigError("max_attempts must be at least 1")
        self.workspace_root = workspace_root
        self.agents = list(agents) if agents is not None else default_pipeline()
        self.max_attempts = max_attempts
        _validate_pipeline(self.agents)

    def create_run(self, kind: SourceKind, target: str) -> tuple[RunContext, RunEventStream]:
        run_id = uuid.uuid4().hex[:12]
        ctx = RunContext(
            run_id=run_id,
            source=redact(target),
            source_kind=kind,
            work_dir=self.workspace_root / run_id,
            stages={
                a.name: StageRecord(name=a.name, title=a.title, timeout_s=a.timeout_s, requires=list(a.requires))
                for a in self.agents
            },
        )
        ctx._fetch_target = target
        events = RunEventStream(ctx.run_id)
        events.publish(RunStatusEvent, status=ctx.status)
        for record in ctx.stages.values():
            events.publish(StageStatusEvent, stage=record.name, status=record.status, attempt=0)
        return ctx, events

    async def execute(self, ctx: RunContext, events: RunEventStream) -> None:
        loop = asyncio.get_running_loop()
        ctx.started_at = utcnow()
        try:
            self._set_run_status(ctx, events, RunStatus.RUNNING)
            for agent in self.agents:
                await self._run_stage(ctx, events, agent, loop)
            self._set_run_status(ctx, events, _final_status(ctx))
        except asyncio.CancelledError:
            self._abandon(ctx, events, "run was cancelled")
            raise
        except Exception as exc:  # a bug in the orchestrator itself, not in an agent
            logger.exception("run %s crashed", ctx.run_id)
            ctx.errors.append(StageError.from_exception(exc, stage=None))
            self._abandon(ctx, events, f"orchestrator error: {exc}")
        finally:
            ctx.finished_at = utcnow()
            events.close()

    async def _run_stage(
        self, ctx: RunContext, events: RunEventStream, agent: Agent, loop: asyncio.AbstractEventLoop
    ) -> None:
        record = ctx.stages[agent.name]
        blocked = [dep for dep in agent.requires if ctx.stages[dep].status is not StageStatus.SUCCESS]
        if blocked:
            names = ", ".join(ctx.stages[dep].title for dep in blocked)
            self._skip(events,record, f"required stage did not succeed: {names}")
            return

        log = StageLogger(events, agent.name, loop)
        record.started_at = utcnow()
        try:
            inp = agent.build_input(ctx)
        except Exception as exc:
            # Building input is deterministic, so a retry would fail the same way.
            self._escalate(ctx, events, record, StageError.from_exception(exc, stage=agent.name))
            return

        for attempt in range(1, self.max_attempts + 1):
            record.attempts = attempt
            self._set_stage_status(events, record, StageStatus.RUNNING)
            try:
                output = await asyncio.wait_for(agent.run(inp, log), timeout=agent.timeout_s)
                ctx.set_output(agent.name, output)
            except TimeoutError:
                error = StageError(
                    stage=agent.name,
                    attempt=attempt,
                    type="TimeoutError",
                    message=f"timed out after {agent.timeout_s:g}s",
                    timed_out=True,
                )
            except Exception as exc:
                error = StageError.from_exception(exc, stage=agent.name, attempt=attempt)
            else:
                record.finished_at = utcnow()
                self._set_stage_status(events, record, StageStatus.SUCCESS)
                return

            if attempt < self.max_attempts:
                record.errors.append(error)
                self._set_stage_status(events, record, StageStatus.FAILED, error=error)
                log.warning(f"Attempt {attempt} failed ({error.type}: {error.message}); retrying")
            else:
                log.error(f"Escalating after {attempt} attempts: {error.type}: {error.message}")
                self._escalate(ctx, events, record, error)

    def _escalate(self, ctx: RunContext, events: RunEventStream, record: StageRecord, error: StageError) -> None:
        record.errors.append(error)
        ctx.errors.append(error)
        record.finished_at = utcnow()
        self._set_stage_status(events, record, StageStatus.ESCALATED, error=error)

    def _skip(self, events: RunEventStream, record: StageRecord, reason: str) -> None:
        record.skipped_reason = reason
        record.finished_at = utcnow()
        self._set_stage_status(events, record, StageStatus.SKIPPED)

    def _abandon(self, ctx: RunContext, events: RunEventStream, reason: str) -> None:
        for record in ctx.stages.values():
            if record.status in (StageStatus.RUNNING, StageStatus.FAILED):
                error = StageError(stage=record.name, attempt=record.attempts, type="Aborted", message=reason)
                self._escalate(ctx, events, record, error)
            elif record.status is StageStatus.PENDING:
                self._skip(events,record, reason)
        self._set_run_status(ctx, events, RunStatus.FAILED, detail=reason)

    @staticmethod
    def _set_stage_status(
        events: RunEventStream, record: StageRecord, status: StageStatus, error: StageError | None = None
    ) -> None:
        record.status = status
        events.publish(
            StageStatusEvent,
            stage=record.name,
            status=status,
            attempt=record.attempts,
            error=error,
            skipped_reason=record.skipped_reason,
        )

    @staticmethod
    def _set_run_status(
        ctx: RunContext, events: RunEventStream, status: RunStatus, detail: str | None = None
    ) -> None:
        ctx.status = status
        events.publish(RunStatusEvent, status=status, detail=detail)


def _final_status(ctx: RunContext) -> RunStatus:
    statuses = [r.status for r in ctx.stages.values()]
    if all(s is StageStatus.SUCCESS for s in statuses):
        return RunStatus.COMPLETED
    if any(s is StageStatus.SUCCESS for s in statuses):
        return RunStatus.COMPLETED_WITH_ESCALATIONS
    return RunStatus.FAILED  # e.g. RepoScout could not fetch the source, so nothing ran


def _validate_pipeline(agents: Sequence[Agent]) -> None:
    seen: set[StageName] = set()
    for agent in agents:
        if agent.name in seen:
            raise PipelineConfigError(f"duplicate stage {agent.name}")
        for dep in (*agent.requires, *agent.uses):
            if dep not in seen:
                raise PipelineConfigError(f"{agent.name} depends on {dep}, which does not run before it")
        if agent.timeout_s <= 0:
            raise PipelineConfigError(f"{agent.name} timeout must be positive")
        seen.add(agent.name)

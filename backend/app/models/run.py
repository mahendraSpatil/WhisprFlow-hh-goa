"""Run state shared by the orchestrator and every agent."""

from __future__ import annotations

import traceback as tb
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel, Field, PrivateAttr

from app.models.agents import (
    Contract,
    DiagnosticSentinelOutput,
    MemoryKeeperOutput,
    PatchMasterOutput,
    PipelineArchitectOutput,
    RegressionCheckOutput,
    RepoScoutOutput,
    RootCauseDiagnosticianOutput,
    SandboxRunnerOutput,
    SourceKind,
    SystemAnalystOutput,
)

T = TypeVar("T", bound=BaseModel)


def utcnow() -> datetime:
    return datetime.now(UTC)


class StageName(StrEnum):
    """Pipeline stages in execution order. Each value is also the RunContext field holding its output."""

    REPO_SCOUT = "repo_scout"
    SYSTEM_ANALYST = "system_analyst"
    PIPELINE_ARCHITECT = "pipeline_architect"
    SANDBOX_RUNNER = "sandbox_runner"
    DIAGNOSTIC_SENTINEL = "diagnostic_sentinel"
    ROOT_CAUSE_DIAGNOSTICIAN = "root_cause_diagnostician"
    PATCH_MASTER = "patch_master"
    REGRESSION_CHECK = "regression_check"
    MEMORY_KEEPER = "memory_keeper"


STAGE_OUTPUT_MODELS: dict[StageName, type[BaseModel]] = {
    StageName.REPO_SCOUT: RepoScoutOutput,
    StageName.SYSTEM_ANALYST: SystemAnalystOutput,
    StageName.PIPELINE_ARCHITECT: PipelineArchitectOutput,
    StageName.SANDBOX_RUNNER: SandboxRunnerOutput,
    StageName.DIAGNOSTIC_SENTINEL: DiagnosticSentinelOutput,
    StageName.ROOT_CAUSE_DIAGNOSTICIAN: RootCauseDiagnosticianOutput,
    StageName.PATCH_MASTER: PatchMasterOutput,
    StageName.REGRESSION_CHECK: RegressionCheckOutput,
    StageName.MEMORY_KEEPER: MemoryKeeperOutput,
}


class StageStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"  # an attempt failed and a retry is coming
    ESCALATED = "escalated"  # all attempts failed; needs a human
    SKIPPED = "skipped"  # a required upstream stage did not succeed


TERMINAL_STAGE_STATUSES = {StageStatus.SUCCESS, StageStatus.ESCALATED, StageStatus.SKIPPED}


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    COMPLETED_WITH_ESCALATIONS = "completed_with_escalations"
    FAILED = "failed"


TERMINAL_RUN_STATUSES = {RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_ESCALATIONS, RunStatus.FAILED}


class StageError(Contract):
    stage: StageName | None = Field(description="None for run-level errors, e.g. an orchestrator crash.")
    attempt: int = 0
    type: str
    message: str
    timed_out: bool = False
    traceback: str | None = None
    at: datetime = Field(default_factory=utcnow)

    @classmethod
    def from_exception(
        cls, exc: BaseException, *, stage: StageName | None, attempt: int = 0, timed_out: bool = False
    ) -> StageError:
        message = str(exc) or ("timed out" if timed_out else type(exc).__name__)
        return cls(
            stage=stage,
            attempt=attempt,
            type=type(exc).__name__,
            message=message,
            timed_out=timed_out,
            traceback=None if timed_out else "".join(tb.format_exception(exc)),
        )


class StageRecord(Contract):
    name: StageName
    title: str
    status: StageStatus = StageStatus.PENDING
    attempts: int = 0
    timeout_s: float
    requires: list[StageName] = []
    started_at: datetime | None = None
    finished_at: datetime | None = None
    errors: list[StageError] = []
    skipped_reason: str | None = None


class MissingStageOutput(RuntimeError):
    pass


class RunContext(BaseModel):
    """Everything known about one run.

    Stages read earlier outputs from here and append their own via ``set_output``.
    Each output field is write-once, so a stage can never overwrite another's result.
    """

    run_id: str
    source: str = Field(description="Repo URL or local path, with any credentials redacted.")
    source_kind: SourceKind
    work_dir: Path = Field(description="Private scratch directory for this run; RepoScout fetches into it.")
    status: RunStatus = RunStatus.QUEUED
    created_at: datetime = Field(default_factory=utcnow)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    stages: dict[StageName, StageRecord] = {}
    errors: list[StageError] = []

    repo_scout: RepoScoutOutput | None = None
    system_analyst: SystemAnalystOutput | None = None
    pipeline_architect: PipelineArchitectOutput | None = None
    sandbox_runner: SandboxRunnerOutput | None = None
    diagnostic_sentinel: DiagnosticSentinelOutput | None = None
    root_cause_diagnostician: RootCauseDiagnosticianOutput | None = None
    patch_master: PatchMasterOutput | None = None
    regression_check: RegressionCheckOutput | None = None
    memory_keeper: MemoryKeeperOutput | None = None

    # The unredacted clone target (may contain a token); never serialized.
    _fetch_target: str = PrivateAttr(default="")

    @property
    def fetch_target(self) -> str:
        return self._fetch_target

    def set_output(self, stage: StageName, output: BaseModel) -> None:
        if getattr(self, stage.value) is not None:
            raise RuntimeError(f"output for {stage} was already recorded")
        expected = STAGE_OUTPUT_MODELS[stage]
        if not isinstance(output, expected):
            raise TypeError(f"{stage} must output {expected.__name__}, got {type(output).__name__}")
        setattr(self, stage.value, output)

    def require(self, stage: StageName, model: type[T]) -> T:
        value = getattr(self, stage.value)
        if value is None:
            raise MissingStageOutput(f"{stage} has no output")
        if not isinstance(value, model):
            raise TypeError(f"{stage} output is {type(value).__name__}, expected {model.__name__}")
        return value

    def optional(self, stage: StageName, model: type[T]) -> T | None:
        value = getattr(self, stage.value)
        return value if isinstance(value, model) else None

    def require_workspace(self) -> Path:
        """The working copy RepoScout fetched; every later stage reads and patches this tree."""
        return self.require(StageName.REPO_SCOUT, RepoScoutOutput).root

    def summary(self) -> RunSummary:
        return RunSummary(
            run_id=self.run_id,
            source=self.source,
            source_kind=self.source_kind,
            status=self.status,
            created_at=self.created_at,
            started_at=self.started_at,
            finished_at=self.finished_at,
            stages=list(self.stages.values()),
            errors=self.errors,
        )


class RunSummary(Contract):
    run_id: str
    source: str
    source_kind: SourceKind
    status: RunStatus
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    stages: list[StageRecord]
    errors: list[StageError]

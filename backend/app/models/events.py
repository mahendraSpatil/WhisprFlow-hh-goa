"""Messages streamed to the frontend over /ws/runs/{run_id}.

Every message carries a per-run ``seq`` starting at 1. A client that reconnects
with ``?since=<last seq>`` receives only what it missed.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from app.models.agents import Contract
from app.models.run import RunStatus, StageError, StageName, StageStatus


class LogLevel(StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"


class EventBase(Contract):
    seq: int
    run_id: str
    ts: datetime


class RunStatusEvent(EventBase):
    type: Literal["run.status"] = "run.status"
    status: RunStatus
    detail: str | None = None


class StageStatusEvent(EventBase):
    type: Literal["stage.status"] = "stage.status"
    stage: StageName
    status: StageStatus
    attempt: int
    error: StageError | None = None
    skipped_reason: str | None = None


class LogEvent(EventBase):
    type: Literal["log"] = "log"
    stage: StageName | None = None
    level: LogLevel
    message: str


RunEvent = Annotated[RunStatusEvent | StageStatusEvent | LogEvent, Field(discriminator="type")]
run_event_adapter: TypeAdapter[RunEvent] = TypeAdapter(RunEvent)

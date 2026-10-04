from __future__ import annotations

from abc import ABC, abstractmethod
from typing import ClassVar, Generic, TypeVar

from pydantic import BaseModel

from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger

InT = TypeVar("InT", bound=BaseModel)
OutT = TypeVar("OutT", bound=BaseModel)


class Agent(ABC, Generic[InT, OutT]):
    """One pipeline stage.

    ``requires`` lists stages that must succeed for this one to run; if any of
    them escalates or is skipped, this stage is skipped. ``uses`` lists stages
    whose output is read when available but whose failure is tolerated.

    ``run`` must treat its input as read-only: the same instance is passed to
    the retry. Blocking work belongs in ``asyncio.to_thread``. Note that a
    timeout cancels the await but cannot stop a thread that is already running.
    """

    name: ClassVar[StageName]
    title: ClassVar[str]
    output_model: ClassVar[type[BaseModel]]
    requires: ClassVar[tuple[StageName, ...]] = ()
    uses: ClassVar[tuple[StageName, ...]] = ()
    default_timeout_s: ClassVar[float] = 60.0

    def __init__(self, timeout_s: float | None = None) -> None:
        self.timeout_s = self.default_timeout_s if timeout_s is None else timeout_s

    @abstractmethod
    def build_input(self, ctx: RunContext) -> InT:
        """Assemble this agent's input contract from the run context."""

    @abstractmethod
    async def run(self, inp: InT, log: StageLogger) -> OutT:
        """Do the work and return this agent's output contract."""

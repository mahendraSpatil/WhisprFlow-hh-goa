from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from app.models.pr import PullRequestResult
from app.models.run import RunContext
from app.orchestrator.events import RunEventStream


@dataclass
class RunHandle:
    context: RunContext
    events: RunEventStream
    task: asyncio.Task[None] | None = None
    pull_request: PullRequestResult | None = None
    pr_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


class RunRegistry:
    """In-memory index of runs for this process. Runs are lost on restart."""

    def __init__(self) -> None:
        self._runs: dict[str, RunHandle] = {}

    def add(self, context: RunContext, events: RunEventStream) -> RunHandle:
        handle = RunHandle(context=context, events=events)
        self._runs[context.run_id] = handle
        return handle

    def get(self, run_id: str) -> RunHandle | None:
        return self._runs.get(run_id)

    async def shutdown(self) -> None:
        tasks = [h.task for h in self._runs.values() if h.task and not h.task.done()]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

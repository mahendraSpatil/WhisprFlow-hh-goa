"""Per-run event stream with replay, and the logger agents write through."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from typing import Any

from app.models.events import EventBase, LogEvent, LogLevel, RunEvent
from app.models.run import StageName, utcnow

logger = logging.getLogger("codeloop")

MAX_LOG_MESSAGE_CHARS = 4000

_PY_LEVELS = {
    LogLevel.DEBUG: logging.DEBUG,
    LogLevel.INFO: logging.INFO,
    LogLevel.WARNING: logging.WARNING,
    LogLevel.ERROR: logging.ERROR,
}


class RunEventStream:
    """Ordered history of one run's events; any number of subscribers can follow it.

    Subscribers read from the shared history by cursor, so a slow WebSocket can
    never block the run or drop events. Must be used from the event loop thread;
    ``StageLogger`` handles hopping over from worker threads.
    """

    def __init__(self, run_id: str) -> None:
        self.run_id = run_id
        self._history: list[RunEvent] = []
        self._changed = asyncio.Event()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def history(self) -> list[RunEvent]:
        return list(self._history)

    def publish(self, event_type: type[EventBase], **fields: Any) -> RunEvent:
        if self._closed:
            raise RuntimeError(f"event stream for run {self.run_id} is closed")
        event = event_type(seq=len(self._history) + 1, run_id=self.run_id, ts=utcnow(), **fields)
        self._history.append(event)  # type: ignore[arg-type]
        self._notify()
        return event  # type: ignore[return-value]

    def close(self) -> None:
        self._closed = True
        self._notify()

    def _notify(self) -> None:
        changed, self._changed = self._changed, asyncio.Event()
        changed.set()

    async def subscribe(self, since: int = 0) -> AsyncIterator[RunEvent]:
        """Yield every event with seq > ``since``, then live events until the run ends."""
        cursor = max(since, 0)
        while True:
            while cursor < len(self._history):
                yield self._history[cursor]
                cursor += 1
            if self._closed:
                return
            await self._changed.wait()


class StageLogger:
    """Log sink handed to agents. Safe to call from worker threads."""

    def __init__(self, events: RunEventStream, stage: StageName | None, loop: asyncio.AbstractEventLoop) -> None:
        self._events = events
        self._stage = stage
        self._loop = loop

    def debug(self, message: str) -> None:
        self._emit(LogLevel.DEBUG, message)

    def info(self, message: str) -> None:
        self._emit(LogLevel.INFO, message)

    def warning(self, message: str) -> None:
        self._emit(LogLevel.WARNING, message)

    def error(self, message: str) -> None:
        self._emit(LogLevel.ERROR, message)

    def _emit(self, level: LogLevel, message: str) -> None:
        if len(message) > MAX_LOG_MESSAGE_CHARS:
            message = message[:MAX_LOG_MESSAGE_CHARS] + "... [truncated]"
        logger.log(_PY_LEVELS[level], "[%s:%s] %s", self._events.run_id, self._stage or "run", message)
        try:
            on_loop = asyncio.get_running_loop() is self._loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            self._publish(level, message)
        else:
            self._loop.call_soon_threadsafe(self._publish, level, message)

    def _publish(self, level: LogLevel, message: str) -> None:
        # A worker thread that outlived its stage's timeout may log after the run ended.
        if not self._events.closed:
            self._events.publish(LogEvent, stage=self._stage, level=level, message=message)

"""Shared helpers: write a tiny repo, then run the real indexing agents over it."""

from __future__ import annotations

import difflib
import textwrap
from pathlib import Path
from types import SimpleNamespace

from app.agents.indexer import index_repository
from app.agents.pipeline_architect import build_graph
from app.agents.system_analyst import analyze
from app.models.agents import (
    Graph,
    PipelineArchitectInput,
    RepoScoutOutput,
    SourceKind,
    SystemAnalystInput,
    SystemAnalystOutput,
)


def write_files(root: Path, files: dict[str, str]) -> None:
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        # lstrip: the snippets start on the line after the opening quotes, so line 1 is the first code line.
        # Bytes, not write_text: on Windows that would write CRLF, and patches are checked against real line endings.
        path.write_bytes(textwrap.dedent(content).lstrip("\n").encode("utf-8"))


def build(root: Path, files: dict[str, str]) -> tuple[RepoScoutOutput, SystemAnalystOutput, Graph]:
    write_files(root, files)
    index = index_repository(root, {"__pycache__"}, 1_000_000)
    repo = RepoScoutOutput(
        root=root, source_kind=SourceKind.LOCAL, modules=index.modules, symbols=index.symbols, calls=index.calls
    )
    system = analyze(SystemAnalystInput(root=root, repo=repo))
    graph = build_graph(PipelineArchitectInput(repo=repo, system=system))
    return repo, system, graph


class Log:
    """Stands in for StageLogger."""

    def __init__(self):
        self.lines: list[str] = []

    def info(self, message): self.lines.append(f"info: {message}")
    def warning(self, message): self.lines.append(f"warning: {message}")
    error = debug = info


def reply(text="ok", stop_reason="end_turn", category=None):
    return SimpleNamespace(
        stop_reason=stop_reason, stop_details=SimpleNamespace(category=category), content=[SimpleNamespace(type="text", text=text)]
    )


class FakeClient:
    """An AsyncAnthropic stand-in. Each call returns (or raises) the next behavior; the last one repeats."""

    def __init__(self, *behaviors):
        self.behaviors, self.calls, self.closed = list(behaviors), [], False
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    async def _create(self, **kwargs):
        self.calls.append(kwargs)
        behavior = self.behaviors.pop(0) if len(self.behaviors) > 1 else self.behaviors[0]
        if callable(behavior) and not isinstance(behavior, Exception):
            behavior = behavior(kwargs)
        if isinstance(behavior, Exception):
            raise behavior
        return behavior

    async def close(self):
        self.closed = True


def status_error(cls, code):
    return cls("denied", response=SimpleNamespace(status_code=code, headers={}, request=None), body=None)


def make_diff(rel: str, old: str, new: str) -> str:
    """A unified diff of old -> new with git-style a/ b/ headers."""
    return "".join(difflib.unified_diff(old.splitlines(True), new.splitlines(True), f"a/{rel}", f"b/{rel}"))

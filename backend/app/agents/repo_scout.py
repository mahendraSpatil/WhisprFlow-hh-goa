from __future__ import annotations

import asyncio
import os
import shutil
import stat
import sys
from pathlib import Path

from git import GitCommandError, InvalidGitRepositoryError, NoSuchPathError, RemoteProgress, Repo

from app.agents.base import Agent
from app.agents.indexer import index_repository
from app.models.agents import CallResolution, RepoScoutInput, RepoScoutOutput, SourceKind
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger
from app.sources import redact, redact_in

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".venv", "venv", "__pycache__", "node_modules", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "*.pyc",
)

# Passed to git through the environment (git >= 2.31) so GitPython's unsafe-option
# guard stays on. Transports that can run local commands are refused, and a clone
# that stalls below 1 KB/s for 30s is aborted by git itself.
_CLONE_CONFIG = {
    "protocol.ext.allow": "never",
    "protocol.file.allow": "never",
    "http.lowSpeedLimit": "1000",
    "http.lowSpeedTime": "30",
}


class FetchError(RuntimeError):
    pass


class RepoScout(Agent[RepoScoutInput, RepoScoutOutput]):
    """Fetches the source into a private working copy, then indexes it with ``ast``."""

    name = StageName.REPO_SCOUT
    title = "RepoScout"
    output_model = RepoScoutOutput
    default_timeout_s = 300.0

    def build_input(self, ctx: RunContext) -> RepoScoutInput:
        return RepoScoutInput(source=ctx.fetch_target, source_kind=ctx.source_kind, dest=ctx.work_dir / "repo")

    async def run(self, inp: RepoScoutInput, log: StageLogger) -> RepoScoutOutput:
        return await asyncio.to_thread(scout, inp, log)


def scout(inp: RepoScoutInput, log: StageLogger) -> RepoScoutOutput:
    source = inp.source.get_secret_value()
    log.info(f"Fetching {inp.source_kind} source {redact(source)}")
    commit, branch = fetch_source(inp.source_kind, source, inp.dest, log)
    log.info(f"Working copy at {inp.dest}" + (f" (commit {commit[:12]})" if commit else ""))

    index = index_repository(inp.dest, set(inp.exclude_dirs), inp.max_file_bytes)
    internal = sum(c.resolution is CallResolution.INTERNAL for c in index.calls)
    external = sum(c.resolution is CallResolution.EXTERNAL for c in index.calls)
    log.info(
        f"Indexed {len(index.modules)} modules, {len(index.symbols)} symbols, {len(index.calls)} calls "
        f"({internal} internal, {external} external, {len(index.calls) - internal - external} unresolved)"
    )
    for err in index.parse_errors:
        log.warning(f"Could not parse {err.path}:{err.line or '?'}: {err.message}")
    if index.skipped_files:
        log.warning(f"Skipped {len(index.skipped_files)} files over {inp.max_file_bytes} bytes")

    return RepoScoutOutput(
        root=inp.dest,
        source_kind=inp.source_kind,
        commit=commit,
        branch=branch,
        modules=index.modules,
        symbols=index.symbols,
        calls=index.calls,
        parse_errors=index.parse_errors,
        skipped_files=index.skipped_files,
    )


# --- Fetching ----------------------------------------------------------------


def fetch_source(kind: SourceKind, source: str, dest: Path, log: StageLogger | None = None) -> tuple[str | None, str | None]:
    """Create ``dest`` from the source and return (commit, branch). Replaces leftovers from a failed attempt."""
    if dest.exists():
        remove_tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if kind is SourceKind.GIT:
        repo = clone_repository(source, dest, log)
        return _head(repo)
    src = Path(source)
    if not src.is_dir():
        raise FetchError(f"local source is not a directory: {source}")
    # symlinks=True copies links as links, so nothing outside the repo is pulled in.
    shutil.copytree(src, dest, ignore=COPY_IGNORE, symlinks=True)
    try:
        return _head(Repo(src, search_parent_directories=True))
    except (InvalidGitRepositoryError, NoSuchPathError):
        return None, None


def clone_repository(url: str, dest: Path, log: StageLogger | None = None, *, allow_local: bool = False) -> Repo:
    """Shallow-clone ``url`` with GitPython. ``allow_local`` permits file:// and path URLs (tests only)."""
    config = dict(_CLONE_CONFIG)
    if allow_local:
        config["protocol.file.allow"] = "always"
    env = {"GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "never", "GIT_CONFIG_COUNT": str(len(config))}
    for i, (key, value) in enumerate(config.items()):
        env[f"GIT_CONFIG_KEY_{i}"] = key
        env[f"GIT_CONFIG_VALUE_{i}"] = value
    try:
        return Repo.clone_from(
            url,
            dest,
            env=env,
            multi_options=["--depth=1", "--single-branch", "--no-tags"],
            progress=_CloneProgress(log) if log else None,
        )
    except GitCommandError as exc:
        stderr = redact_in(str(exc.stderr or "").strip(), url)
        # "from None": the original exception's message contains the unredacted URL.
        raise FetchError(f"git clone of {redact(url)} failed (exit {exc.status}): {stderr[-1000:]}") from None


def _head(repo: Repo) -> tuple[str | None, str | None]:
    try:
        commit = repo.head.commit.hexsha
    except ValueError:  # repo with no commits
        return None, None
    branch = None if repo.head.is_detached else repo.active_branch.name
    return commit, branch


def remove_tree(path: Path) -> None:
    """rmtree that also removes read-only files, which git creates on Windows."""

    def make_writable(func, target, _exc) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=make_writable)
    else:
        shutil.rmtree(path, onerror=make_writable)


class _CloneProgress(RemoteProgress):
    _STAGES = {
        RemoteProgress.COUNTING: "Counting objects",
        RemoteProgress.COMPRESSING: "Compressing objects",
        RemoteProgress.RECEIVING: "Receiving objects",
        RemoteProgress.RESOLVING: "Resolving deltas",
        RemoteProgress.CHECKING_OUT: "Checking out files",
    }

    def __init__(self, log: StageLogger) -> None:
        super().__init__()
        self._log = log

    def update(self, op_code: int, cur_count, max_count=None, message: str = "") -> None:
        if op_code & self.END and (stage := self._STAGES.get(op_code & self.OP_MASK)):
            total = f" ({int(max_count)})" if max_count else ""
            self._log.info(f"{stage}: done{total}")

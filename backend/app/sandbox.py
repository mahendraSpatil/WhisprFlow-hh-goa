"""Running a repo's tests in a sandbox: a fresh virtualenv, a private copy, a timeout, no network.

This is subprocess isolation, and it is best effort. What it does:

- the tests run from a *copy* of the repo, in a virtualenv created for the run
- only the repo's declared dependencies (plus pytest) are installed, never the repo itself
- the environment is stripped of credentials, and proxies point at a dead port
- inside the interpreter, ``codeloop_harness.guard`` blocks non-loopback sockets and DNS and
  writes outside the copy and the temp dir
- the whole process tree is killed on timeout

What it does not do: native code, or a determined test, can step around the in-process guard,
and installing dependencies runs their build scripts. A container (``--network none``, memory and
process limits) would be a real boundary; that mode is not implemented.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from app.models.agents import (
    CaseOutcome,
    CaseResult,
    ExceptionInfo,
    Isolation,
    SandboxRunnerOutput,
    SuiteTotals,
)
from app.orchestrator.events import StageLogger

HARNESS_DIR = Path(__file__).parent / "sandbox_harness"
READY_MARKER = ".codeloop-ready"
INSTALL_TIMEOUT_S = 600
DEFAULT_TEST_TIMEOUT_S = 300.0
EVENT_PREFIX = "@@codeloop "

SECRET_NAME = re.compile(r"(KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|AUTH)", re.IGNORECASE)
SECRET_PREFIXES = ("ANTHROPIC_", "AWS_", "AZURE_", "GITHUB_", "GH_", "GOOGLE_", "OPENAI_", "CODELOOP_")
DEAD_PROXY = "http://127.0.0.1:9"

COPY_IGNORE = shutil.ignore_patterns(
    ".git", ".hg", ".venv", "venv", "__pycache__", "node_modules", ".tox", ".nox",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", "*.pyc",
)

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


@dataclass(frozen=True)
class Sandbox:
    python: Path
    isolation: Isolation


def clean_environment() -> dict[str, str]:
    """The current environment without anything that looks like a credential."""
    return {
        k: v
        for k, v in os.environ.items()
        if not SECRET_NAME.search(k) and not k.upper().startswith(SECRET_PREFIXES)
    }


def remove_tree(path: Path) -> None:
    """rmtree that also removes read-only files, which git and pip create on Windows."""

    def make_writable(func, target, _exc) -> None:
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if path.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=make_writable)
        else:
            shutil.rmtree(path, onerror=make_writable)


def fresh_copy(src: Path, dest: Path) -> Path:
    """A clean private copy of the repo; replaces anything already at ``dest``."""
    remove_tree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(src, dest, ignore=COPY_IGNORE, symlinks=True)  # links stay links: nothing outside is pulled in
    return dest


def _lock_for(path: Path) -> threading.Lock:
    with _locks_guard:
        return _locks.setdefault(str(path), threading.Lock())


def _run(cmd: list[str], timeout: float, env: dict[str, str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True, errors="replace", env=env, timeout=timeout, check=False)


def ensure_environment(work_dir: Path, dependencies: list[str], log: StageLogger) -> Sandbox:
    """The interpreter the tests run under: a virtualenv in ``work_dir``, created once and reused.

    ``CODELOOP_SANDBOX_PYTHON`` names an interpreter to use instead (the tests use it, to avoid
    building a virtualenv for every case).
    """
    override = os.environ.get("CODELOOP_SANDBOX_PYTHON")
    if override:
        python = Path(override).expanduser()
        if not python.exists() or not python.is_file():
            raise RuntimeError(f"CODELOOP_SANDBOX_PYTHON does not resolve to an executable: {override!r}")
        return Sandbox(python.resolve(), Isolation.SUBPROCESS)

    venv = work_dir / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    with _lock_for(venv):
        if (venv / READY_MARKER).exists() and python.exists() and python.is_file():
            return Sandbox(python.resolve(), Isolation.SUBPROCESS)
        remove_tree(venv)
        log.info("Creating a fresh virtualenv for the sandbox")
        created = _run([sys.executable, "-m", "venv", str(venv)], INSTALL_TIMEOUT_S, clean_environment())
        if created.returncode != 0:
            raise RuntimeError(f"could not create a virtualenv: {created.stderr.strip()[-500:]}")

        packages = sorted({"pytest", *dependencies})
        log.info(f"Installing {len(packages)} packages: {', '.join(packages)}")
        install = [str(python), "-m", "pip", "install", "--disable-pip-version-check", "--no-input", "-q"]
        result = _run([*install, *packages], INSTALL_TIMEOUT_S, clean_environment())
        if result.returncode != 0:
            log.warning(f"pip could not install everything at once; trying each package: {result.stderr.strip()[-300:]}")
            for package in packages:
                one = _run([*install, package], INSTALL_TIMEOUT_S, clean_environment())
                if one.returncode != 0:
                    log.warning(f"could not install {package}: {one.stderr.strip().splitlines()[-1:] or ['unknown error']}")
        (venv / READY_MARKER).write_text("ready", encoding="utf-8")
        return Sandbox(python, Isolation.SUBPROCESS)


def kill_tree(process: subprocess.Popen) -> None:
    try:
        if os.name == "nt":
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(process.pid)], capture_output=True, check=False)
        else:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
    except (OSError, ProcessLookupError):
        process.kill()


def _read_events(path: Path) -> list[dict]:
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith(EVENT_PREFIX):
            try:
                events.append(json.loads(line[len(EVENT_PREFIX) :]))
            except json.JSONDecodeError:
                continue  # a line cut off by a kill
    return events


def run_suite(
    sandbox: Sandbox, repo: Path, test_paths: list[str], log: StageLogger, timeout_s: float = DEFAULT_TEST_TIMEOUT_S
) -> SandboxRunnerOutput:
    """Run the repo's pytest suite from ``repo`` (a private copy) and return per-test results."""
    events_file = repo.parent / "events.jsonl"
    events_file.unlink(missing_ok=True)
    src = repo / "src"
    pythonpath = os.pathsep.join([str(HARNESS_DIR), str(repo), *([str(src)] if src.is_dir() else [])])
    env = {
        **clean_environment(),
        "PYTHONPATH": pythonpath,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONUTF8": "1",
        "CODELOOP_REPO_ROOT": str(repo),
        "CODELOOP_EVENTS": str(events_file),
        "HTTP_PROXY": DEAD_PROXY, "HTTPS_PROXY": DEAD_PROXY, "ALL_PROXY": DEAD_PROXY,
        "http_proxy": DEAD_PROXY, "https_proxy": DEAD_PROXY, "all_proxy": DEAD_PROXY,
        "NO_PROXY": "127.0.0.1,localhost", "no_proxy": "127.0.0.1,localhost",
    }
    # -o addopts= : the repo's own addopts may need plugins (coverage, xdist) that are not installed here.
    # --continue-on-collection-errors: one module that fails to import must not hide every other test.
    cmd = [
        str(sandbox.python), "-m", "pytest", "-p", "codeloop_harness.pytest_plugin", "-q",
        "-p", "no:cacheprovider", "-o", "addopts=", "--continue-on-collection-errors",
        *[p for p in test_paths if p not in ("", ".")],
    ]
    flags = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP} if os.name == "nt" else {"start_new_session": True}
    started = time.monotonic()
    process = subprocess.Popen(
        cmd, cwd=repo, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, errors="replace", **flags
    )
    timed_out = False
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        kill_tree(process)
        try:
            stdout, stderr = process.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            stdout, stderr = "", ""
    duration = time.monotonic() - started

    events = _read_events(events_file)
    tests = [
        CaseResult(
            node_id=e["node_id"],
            outcome=CaseOutcome(e["outcome"]),
            duration_s=float(e.get("duration") or 0.0),
            message=e.get("message"),
            exception=ExceptionInfo(**e["exception"]) if e.get("exception") else None,
        )
        for e in events
        if e.get("event") == "test"
    ]
    finished = any(e.get("event") == "session.finish" for e in events)
    code = process.returncode
    if timed_out:
        tests.append(CaseResult(node_id="<timeout>", outcome=CaseOutcome.ERROR, message=f"pytest timed out after {timeout_s:g}s"))
        log.warning(f"Tests timed out after {timeout_s:g}s; keeping the {len(tests) - 1} results that arrived")
    elif not finished and code not in (0, 1, 5):
        tail = (stderr.strip() or stdout.strip())[-600:]
        tests.append(CaseResult(node_id="<pytest>", outcome=CaseOutcome.ERROR, message=f"pytest exited with {code}: {tail}"))
        log.warning(f"pytest exited with {code} before finishing: {tail[-200:]}")

    totals = SuiteTotals(
        passed=sum(t.outcome is CaseOutcome.PASSED for t in tests),
        failed=sum(t.outcome is CaseOutcome.FAILED for t in tests),
        errors=sum(t.outcome is CaseOutcome.ERROR for t in tests),
        skipped=sum(t.outcome is CaseOutcome.SKIPPED for t in tests),
    )
    return SandboxRunnerOutput(isolation=sandbox.isolation, tests=tests, totals=totals, duration_s=round(duration, 2))

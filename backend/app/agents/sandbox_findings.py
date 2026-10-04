"""Findings from exceptions and tracebacks captured by SandboxRunner (failing tests and probes)."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from app.models.agents import (
    CaseOutcome,
    ExceptionInfo,
    RepoScoutOutput,
    SandboxRunnerOutput,
    Severity,
    SourceLocation,
    StackFrame,
)

SANDBOX_GUARD_ERRORS = {"SandboxViolation"}
COLLECTION_ERRORS = {"ImportError", "ModuleNotFoundError", "SyntaxError", "IndentationError"}
SQL_ERRORS = {"OperationalError", "ProgrammingError", "DatabaseError"}
SQL_SYNTAX_HINTS = ("syntax error", "unrecognized token", "unterminated", "incomplete input")


@dataclass
class SandboxFailure:
    exception_type: str
    message: str
    location: SourceLocation
    frames: list[StackFrame]
    origin: str  # test node id or probe target
    in_app_code: bool
    count: int
    other_origins: list[str]
    severity: Severity
    sql_syntax: bool


def _short_type(name: str) -> str:
    return name.rsplit(".", 1)[-1]


def _severity(exc_type: str, in_app_code: bool) -> Severity:
    if exc_type in SANDBOX_GUARD_ERRORS:
        return Severity.LOW  # the sandbox did its job; worth knowing the code reaches for the network or disk
    if exc_type in COLLECTION_ERRORS or exc_type == "AssertionError" or exc_type == "TimeoutError":
        return Severity.MEDIUM
    return Severity.HIGH if in_app_code else Severity.MEDIUM


def collect_failures(sandbox: SandboxRunnerOutput, repo: RepoScoutOutput) -> list[SandboxFailure]:
    """Failures grouped by (exception type, offending line): the same bug seen by ten tests is one finding."""
    test_files = {m.path for m in repo.modules if m.is_test}
    raw: list[tuple[str, str, ExceptionInfo | None, str | None]] = []
    for case in sandbox.tests:
        if case.outcome in (CaseOutcome.FAILED, CaseOutcome.ERROR):
            raw.append((case.node_id, "test", case.exception, case.message))
    for probe in sandbox.probes:
        if probe.outcome in (CaseOutcome.FAILED, CaseOutcome.ERROR):
            raw.append((f"{probe.target}({probe.inputs})", "probe", probe.exception, None))

    groups: dict[tuple[str, str, int], list[tuple[str, ExceptionInfo | None, str | None]]] = defaultdict(list)
    for origin, kind, exc, message in raw:
        exc_type = _short_type(exc.type) if exc else "Error"
        app_frames = [f for f in (exc.frames if exc else []) if f.file not in test_files]
        if app_frames:
            where = app_frames[-1]  # innermost frame in application code
        elif exc and exc.frames:
            where = exc.frames[-1]
        else:
            file = origin.split("::", 1)[0] if kind == "test" else "<unknown>"
            where = StackFrame(file=file, line=1, function="<module>")
        groups[(exc_type, where.file, where.line)].append((origin, exc, message))

    failures = []
    for (exc_type, file, line), members in groups.items():
        origin, exc, message = members[0]
        frames = exc.frames if exc else []
        in_app = file not in test_files and file != "<unknown>"
        text = exc.message if exc else (message or "")
        failures.append(
            SandboxFailure(
                exception_type=exc_type,
                message=text,
                location=SourceLocation(file=file, line=line),
                frames=frames,
                origin=origin,
                in_app_code=in_app,
                count=len(members),
                other_origins=[m[0] for m in members[1:6]],
                severity=_severity(exc_type, in_app),
                sql_syntax=exc_type in SQL_ERRORS and any(h in text.lower() for h in SQL_SYNTAX_HINTS),
            )
        )
    return failures

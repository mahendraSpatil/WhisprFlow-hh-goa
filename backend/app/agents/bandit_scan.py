"""Security findings from a bandit scan, mapped to OWASP Top 10 categories.

Bandit parses source with ``ast``; it never imports or runs the target code, so
scanning an untrusted repo is safe.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from app.agents.owasp import owasp_category
from app.models.agents import Severity

SCAN_TIMEOUT_S = 120

_RANK = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]
_BASE = {"HIGH": Severity.HIGH, "MEDIUM": Severity.MEDIUM, "LOW": Severity.LOW}


@dataclass(frozen=True)
class BanditIssue:
    file: str  # relative to the scanned root, POSIX separators
    line: int
    end_line: int
    test_id: str
    test_name: str
    text: str
    severity: Severity
    confidence: str  # HIGH | MEDIUM | LOW, as bandit reports it
    bandit_severity: str
    cwe: int | None
    owasp: str | None
    code: str


class BanditError(RuntimeError):
    pass


def severity_for(bandit_severity: str, confidence: str, owasp: str | None = None) -> Severity:
    """Bandit's severity, one level down when it is unsure (never below LOW).

    Injection (A03) is high-impact even when bandit cannot tell whether the value is attacker
    controlled, so it is floored at MEDIUM, and high-severity high-confidence injection or code
    execution is CRITICAL.
    """
    level = _RANK.index(_BASE.get(bandit_severity.upper(), Severity.LOW))
    if confidence.upper() == "LOW":
        level -= 1
    severity = _RANK[max(1, level)]
    if owasp == "A03:2021":
        severity = max(severity, Severity.MEDIUM, key=_RANK.index)
        if bandit_severity.upper() == "HIGH" and confidence.upper() == "HIGH":
            severity = Severity.CRITICAL
    return severity


def run_bandit(root: Path, exclude_dirs: list[str]) -> list[BanditIssue]:
    excludes = ",".join(f"*/{d}/*" for d in exclude_dirs)
    cmd = [sys.executable, "-m", "bandit", "-r", ".", "-f", "json", "--exit-zero", "-x", excludes]
    env = {**os.environ, "PYTHONUTF8": "1"}
    try:
        result = subprocess.run(
            cmd, cwd=root, capture_output=True, text=True, encoding="utf-8", env=env,
            timeout=SCAN_TIMEOUT_S, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise BanditError(f"bandit timed out after {SCAN_TIMEOUT_S}s") from exc
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        tail = (result.stderr or result.stdout).strip()[-500:]
        raise BanditError(f"bandit produced no JSON report (exit {result.returncode}): {tail}") from exc

    issues = []
    for r in report.get("results", []):
        cwe = (r.get("issue_cwe") or {}).get("id")
        lines = r.get("line_range") or [r["line_number"]]
        issues.append(
            BanditIssue(
                file=Path(r["filename"]).as_posix().removeprefix("./"),
                line=r["line_number"],
                end_line=max(lines),
                test_id=r["test_id"],
                test_name=r["test_name"],
                text=r["issue_text"],
                severity=severity_for(r["issue_severity"], r["issue_confidence"], owasp_category(r["test_id"], cwe)),
                confidence=r["issue_confidence"],
                bandit_severity=r["issue_severity"],
                cwe=cwe,
                owasp=owasp_category(r["test_id"], cwe),
                code=r.get("code", ""),
            )
        )
    return issues

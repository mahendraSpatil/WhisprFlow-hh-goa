"""Incident memory: signatures for findings, and the SQLite store that remembers them across runs.

A signature is a hash of three things: the exception type (or the rule id, for findings that are not
exceptions), the *normalized code pattern* at the offending line, and the OWASP category. The
pattern is the offending statement with the details that vary between codebases removed, so the
same mistake written with different names matches:

    query = f"SELECT ... WHERE name = '{customer}'"    ->  _ = fstr(_)
    sql = f"DELETE FROM t WHERE id = {item_id}"        ->  _ = fstr(_)
    self._stock[sku] = current - quantity              ->  _._stock[_] = _ - _

Variables become ``_``; string and number literals become STR and NUM; f-strings become
``fstr(<values>)``. Builtins, attribute and method names, operators, True/False/None and keyword
argument names are kept, because they are what makes the pattern the pattern.

Who touches the store: DiagnosticSentinel and PatchMaster read it (to raise severity for a pattern
seen before, and to show Claude fixes that worked); only MemoryKeeper writes to it.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import json
import os
import re
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from app.models.agents import Finding, Severity
from app.models.memory import Incident, PastFix

MAX_PATTERN_NODES = 24
MAX_DIFF_CHARS = 6000
BUILTIN_NAMES = frozenset(dir(builtins))
COMPOUND = (
    ast.If, ast.For, ast.AsyncFor, ast.While, ast.With, ast.AsyncWith, ast.Try, ast.FunctionDef,
    ast.AsyncFunctionDef, ast.ClassDef, ast.Match,
)
SEVERITY_LADDER = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH, Severity.CRITICAL]


# --- Normalizing code -----------------------------------------------------------------------------


class _Collapse(ast.NodeTransformer):
    def visit_Name(self, node: ast.Name) -> ast.AST:
        return node if node.id in BUILTIN_NAMES else ast.Name(id="_", ctx=node.ctx)

    def visit_arg(self, node: ast.arg) -> ast.AST:
        return ast.arg(arg="_", annotation=self.visit(node.annotation) if node.annotation else None)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        value = node.value
        if value is None or value is Ellipsis or isinstance(value, bool):
            return node
        return ast.Name(id="STR" if isinstance(value, str) else "BYTES" if isinstance(value, bytes) else "NUM", ctx=ast.Load())

    def visit_JoinedStr(self, node: ast.JoinedStr) -> ast.AST:
        values = [self.visit(v.value) for v in node.values if isinstance(v, ast.FormattedValue)]
        return ast.Call(func=ast.Name(id="fstr", ctx=ast.Load()), args=values, keywords=[])


def _size(node: ast.AST) -> int:
    return sum(1 for _ in ast.walk(node))


def _innermost_statement(tree: ast.AST, line: int) -> ast.stmt | None:
    best: ast.stmt | None = None

    def walk(node: ast.AST) -> None:
        nonlocal best
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt) and child.lineno <= line <= (child.end_lineno or child.lineno):
                best = child
                walk(child)

    walk(tree)
    return best


def _fallback(source: str, line: int) -> str | None:
    """For code that does not parse: the line itself with literals collapsed."""
    lines = source.splitlines()
    if not 1 <= line <= len(lines):
        return None
    text = re.sub(r"\"[^\"\n]*\"|'[^'\n]*'", "STR", lines[line - 1].strip())
    text = re.sub(r"\b\d+(?:\.\d+)?\b", "NUM", text)
    return " ".join(text.split()) or None


def normalize_pattern(source: str, line: int) -> str | None:
    """The normalized code pattern at ``line`` of ``source``; None when there is nothing there."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return _fallback(source, line)
    statement = _innermost_statement(tree, line)
    node: ast.AST | None = None
    if statement is not None and not isinstance(statement, COMPOUND) and _size(statement) <= MAX_PATTERN_NODES:
        node = statement
    else:
        # A long or compound statement (a big return dict, an `if` header): use the largest single-line
        # expression on the offending line instead.
        candidates = [
            n for n in ast.walk(statement or tree)
            if isinstance(n, ast.expr) and getattr(n, "lineno", None) == line and n.end_lineno == line and _size(n) <= MAX_PATTERN_NODES
        ]
        node = max(candidates, key=_size, default=None)
    if node is None:
        return _fallback(source, line)
    collapsed = ast.fix_missing_locations(_Collapse().visit(copy.deepcopy(node)))
    return " ".join(ast.unparse(collapsed).split())


# --- Signatures -----------------------------------------------------------------------------------


def kind_of(finding: Finding) -> str:
    """The exception type, or the rule id for findings that are not exceptions (B608, RACE003...)."""
    return finding.exception.type.rsplit(".", 1)[-1] if finding.exception else finding.rule_id


def compute_signature(kind: str, pattern: str, owasp: str | None) -> str:
    return hashlib.sha256(f"{kind}\n{pattern}\n{owasp or ''}".encode()).hexdigest()[:16]


def attach_signatures(findings: list[Finding], root: Path, test_files: set[str]) -> list[Finding]:
    """Findings with their signature and pattern filled in. Findings in test code are left unsigned:
    they are symptoms, and remembering them would bury the patterns worth remembering."""
    base = root.resolve()
    texts: dict[str, str | None] = {}

    def text(file: str) -> str | None:
        if file not in texts:
            path = (base / file).resolve()
            try:
                inside = base in path.parents and path.is_file() and path.stat().st_size <= 1_000_000
                texts[file] = path.read_text(encoding="utf-8", errors="replace") if inside else None
            except OSError:
                texts[file] = None
        return texts[file]

    signed = []
    for f in findings:
        source = None if f.location.file in test_files else text(f.location.file)
        pattern = normalize_pattern(source, f.location.line) if source is not None else None
        if pattern is None:
            signed.append(f)
            continue
        signed.append(f.model_copy(update={"pattern": pattern, "signature": compute_signature(kind_of(f), pattern, f.owasp)}))
    return signed


def boosted(severity: Severity) -> Severity:
    """One level up the ladder, capped at critical."""
    return SEVERITY_LADDER[min(SEVERITY_LADDER.index(severity) + 1, len(SEVERITY_LADDER) - 1)]


# --- The store ------------------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL,
    run_id TEXT NOT NULL,
    finding_id TEXT NOT NULL,
    source TEXT NOT NULL,
    created_at TEXT NOT NULL,
    kind TEXT NOT NULL,
    pattern TEXT NOT NULL,
    owasp TEXT,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    rule_id TEXT NOT NULL,
    title TEXT NOT NULL,
    file TEXT NOT NULL,
    line INTEGER NOT NULL,
    symbol TEXT,
    root_cause TEXT,
    patch_status TEXT NOT NULL DEFAULT 'none',
    patch_diff TEXT,
    patch_design TEXT,
    regression_passed INTEGER,
    regression_reasons TEXT NOT NULL DEFAULT '[]',
    UNIQUE (run_id, finding_id)
);
CREATE INDEX IF NOT EXISTS idx_incidents_signature ON incidents (signature);
CREATE INDEX IF NOT EXISTS idx_incidents_kind ON incidents (kind, owasp);
"""


@dataclass
class NewIncident:
    signature: str
    run_id: str
    finding_id: str
    source: str
    kind: str
    pattern: str
    owasp: str | None
    category: str
    severity: str
    rule_id: str
    title: str
    file: str
    line: int
    symbol: str | None = None
    root_cause: str | None = None
    patch_status: str = "none"
    patch_diff: str | None = None
    patch_design: str | None = None
    regression_passed: bool | None = None
    regression_reasons: tuple[str, ...] = ()


def default_path() -> Path:
    configured = os.environ.get("CODELOOP_MEMORY_DB")
    return Path(configured) if configured else Path.home() / ".codeloop" / "memory.db"


def memory_enabled() -> bool:
    """CODELOOP_MEMORY=off: nothing is read from or written to the incident memory."""
    return os.environ.get("CODELOOP_MEMORY", "on").lower() not in ("off", "0", "false")


class MemoryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)

    @classmethod
    def default(cls) -> MemoryStore | None:
        return cls(default_path()) if memory_enabled() else None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=15)
        conn.row_factory = sqlite3.Row
        try:
            with conn:  # commits on success, rolls back on error
                yield conn
        finally:
            conn.close()

    # -- reads

    def seen_counts(self, signatures: Iterable[str], exclude_run: str | None = None) -> dict[str, int]:
        """For each signature, in how many runs (other than ``exclude_run``) it was already recorded."""
        wanted = sorted({s for s in signatures if s})
        if not wanted:
            return {}
        marks = ",".join("?" * len(wanted))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT signature, COUNT(DISTINCT run_id) AS runs FROM incidents "
                f"WHERE signature IN ({marks}) AND run_id != ? GROUP BY signature",
                [*wanted, exclude_run or ""],
            ).fetchall()
        return {r["signature"]: r["runs"] for r in rows}

    def past_fixes(
        self, signature: str, kind: str, owasp: str | None, exclude_run: str | None = None, limit: int = 3
    ) -> list[PastFix]:
        """Fixes that passed regression for this signature (exact) or the same kind and OWASP category (similar).

        Only accepted fixes are ever offered, exact matches first, then newest; identical diffs are shown once.
        """
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM incidents WHERE patch_status = 'valid' AND regression_passed = 1 AND patch_diff IS NOT NULL "
                "AND patch_diff != '' AND run_id != ? AND (signature = ? OR (kind = ? AND COALESCE(owasp, '') = ?)) "
                "ORDER BY (signature = ?) DESC, id DESC LIMIT ?",
                [exclude_run or "", signature, kind, owasp or "", signature, limit * 6],
            ).fetchall()
        fixes: list[PastFix] = []
        seen_diffs: set[str] = set()
        for r in rows:
            if r["patch_diff"] in seen_diffs:
                continue
            seen_diffs.add(r["patch_diff"])
            fixes.append(
                PastFix(
                    incident_id=r["id"], match="exact" if r["signature"] == signature else "similar", seen_at=r["created_at"],
                    kind=r["kind"], owasp=r["owasp"], root_cause=r["root_cause"] or "", diff=r["patch_diff"][:MAX_DIFF_CHARS],
                    design=r["patch_design"] or "",
                )
            )
            if len(fixes) == limit:
                break
        return fixes

    def occurrences(self, signature: str) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM incidents WHERE signature = ?", [signature]).fetchone()[0]

    def list_incidents(self, limit: int = 500) -> list[Incident]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT i.*, (SELECT COUNT(*) FROM incidents j WHERE j.signature = i.signature) AS occurrences "
                "FROM incidents i ORDER BY i.id DESC LIMIT ?",
                [limit],
            ).fetchall()
        return [_to_incident(r) for r in rows]

    def count(self) -> int:
        with self._connect() as conn:
            return conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]

    # -- writes

    def record(self, incident: NewIncident) -> int:
        """Store an incident; recording the same finding of the same run again replaces it (stage retries)."""
        values = {
            **incident.__dict__,
            "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "regression_passed": None if incident.regression_passed is None else int(incident.regression_passed),
            "regression_reasons": json.dumps(list(incident.regression_reasons)),
        }
        columns = list(values)
        updates = ",".join(f"{c}=excluded.{c}" for c in columns if c not in ("run_id", "finding_id"))
        with self._connect() as conn:
            conn.execute(
                f"INSERT INTO incidents ({','.join(columns)}) VALUES ({','.join('?' * len(columns))}) "
                f"ON CONFLICT (run_id, finding_id) DO UPDATE SET {updates}",
                list(values.values()),
            )
            return conn.execute(
                "SELECT id FROM incidents WHERE run_id = ? AND finding_id = ?", [incident.run_id, incident.finding_id]
            ).fetchone()[0]


def _to_incident(row: sqlite3.Row) -> Incident:
    return Incident(
        id=row["id"], signature=row["signature"], run_id=row["run_id"], source=row["source"], created_at=row["created_at"],
        kind=row["kind"], pattern=row["pattern"], owasp=row["owasp"], category=row["category"], severity=row["severity"],
        rule_id=row["rule_id"], title=row["title"], file=row["file"], line=row["line"], symbol=row["symbol"],
        root_cause=row["root_cause"], patch_status=row["patch_status"], patch_diff=row["patch_diff"],
        patch_design=row["patch_design"],
        regression_passed=None if row["regression_passed"] is None else bool(row["regression_passed"]),
        regression_reasons=json.loads(row["regression_reasons"] or "[]"),
        occurrences=row["occurrences"] if "occurrences" in row.keys() else 1,
    )


def open_memory(log) -> MemoryStore | None:
    """The default store, or None when memory is switched off or the database cannot be opened.

    A broken memory must never stop an analysis: the agents that only read it carry on without it.
    """
    try:
        return MemoryStore.default()
    except (sqlite3.Error, OSError) as exc:
        log.warning(f"Incident memory is unavailable ({type(exc).__name__}: {exc}); continuing without it")
        return None

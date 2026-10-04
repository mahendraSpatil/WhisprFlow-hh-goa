"""Input and output contracts for the nine CodeLoop agents.

Each agent consumes exactly one ``*Input`` model and produces exactly one
``*Output`` model. Agents never read each other's outputs directly: an agent's
input is built from the shared RunContext, which holds every earlier output.
"""

from __future__ import annotations

from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid")


# --- Shared types -----------------------------------------------------------


class SourceKind(StrEnum):
    GIT = "git"
    LOCAL = "local"


class Severity(StrEnum):
    INFO = "info"
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SourceLocation(Contract):
    file: str = Field(description="Path relative to the repo root, POSIX separators.")
    line: int = Field(ge=1)
    end_line: int | None = Field(default=None, ge=1)
    column: int | None = Field(default=None, ge=0)


class StackFrame(Contract):
    file: str
    line: int
    function: str


class ExceptionInfo(Contract):
    type: str
    message: str
    frames: list[StackFrame] = []


TestRunner = Literal["pytest", "unittest"]


class CaseOutcome(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"
    SKIPPED = "skipped"


class SuiteTotals(Contract):
    passed: int = 0
    failed: int = 0
    errors: int = 0
    skipped: int = 0


# --- 1. RepoScout -----------------------------------------------------------

DEFAULT_EXCLUDE_DIRS = [
    ".git", ".hg", ".venv", "venv", "env", "__pycache__", "node_modules",
    "build", "dist", ".tox", ".nox", ".eggs", ".mypy_cache", ".pytest_cache",
    "site-packages",
]


class SymbolKind(StrEnum):
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"


class Symbol(Contract):
    qualname: str = Field(description='Dotted path, e.g. "pkg.mod.Class.method".')
    name: str
    kind: SymbolKind
    module: str
    location: SourceLocation
    is_async: bool = False
    decorators: list[str] = []
    parent: str | None = Field(default=None, description="Qualname of the enclosing class or function.")
    bases: list[str] = Field(
        default=[], description="Classes only: base classes, as qualnames when internal, else dotted names."
    )
    complexity: int | None = Field(default=None, description="Functions only: cyclomatic complexity.")
    references: list[str] = Field(
        default=[],
        description="Functions only: symbols and library names used without being called "
        "(callbacks, annotations), as qualnames or dotted names.",
    )


class ImportRef(Contract):
    module: str = Field(description="Absolute module name; relative imports are resolved.")
    names: list[str] = Field(default=[], description="Names imported by a from-import.")
    level: int = 0
    line: int


class CallResolution(StrEnum):
    INTERNAL = "internal"  # target is a Symbol qualname in this repo
    EXTERNAL = "external"  # target is a dotted name in a library, the stdlib or builtins
    UNRESOLVED = "unresolved"  # target could not be determined statically


class CallSite(Contract):
    caller: str = Field(description="Qualname of the enclosing function, or the module name for top-level code.")
    callee: str = Field(description='Source text of the call target, e.g. "self.repo.save".')
    target: str | None = Field(
        default=None, description='Resolved target, e.g. "pkg.repo.Repo.save" or "sqlite3.Connection.execute".'
    )
    resolution: CallResolution = CallResolution.UNRESOLVED
    location: SourceLocation


class ModuleInfo(Contract):
    name: str
    path: str
    is_package: bool
    is_test: bool
    main_guard_line: int | None = None
    loc: int
    imports: list[ImportRef] = []
    depends_on: list[str] = Field(default=[], description="Internal modules this module imports.")


class ParseError(Contract):
    path: str
    message: str
    line: int | None = None


class RepoScoutInput(Contract):
    source: SecretStr = Field(description="Git URL or local path. Secret because a URL may embed a token.")
    source_kind: SourceKind
    dest: Path = Field(description="Where the working copy is created; replaced if it already exists.")
    exclude_dirs: list[str] = DEFAULT_EXCLUDE_DIRS
    max_file_bytes: int = 1_000_000


class RepoScoutOutput(Contract):
    root: Path = Field(description="The working copy every later stage reads and patches.")
    source_kind: SourceKind
    commit: str | None = Field(default=None, description="HEAD commit of the source, when it is a git repo.")
    branch: str | None = None
    modules: list[ModuleInfo] = []
    symbols: list[Symbol] = []
    calls: list[CallSite] = []
    parse_errors: list[ParseError] = []
    skipped_files: list[str] = Field(default=[], description="Files over max_file_bytes.")


# --- 2. SystemAnalyst -------------------------------------------------------


class EntrypointKind(StrEnum):
    MAIN_GUARD = "main_guard"
    WEB_ROUTE = "web_route"
    CLI_COMMAND = "cli_command"


class Entrypoint(Contract):
    kind: EntrypointKind
    module: str
    symbol: str | None = None
    location: SourceLocation
    detail: str | None = Field(default=None, description='e.g. the route decorator "app.get(\'/items\')".')


class ArchitectureStyle(StrEnum):
    WEB_SERVICE = "web_service"
    WORKER = "worker"
    CLI = "cli"
    LIBRARY = "library"
    UNKNOWN = "unknown"


class StackCategory(StrEnum):
    WEB_FRAMEWORK = "web_framework"
    WEB_SERVER = "web_server"
    HTTP_CLIENT = "http_client"
    DATABASE = "database"
    ORM = "orm"
    CACHE = "cache"
    CONCURRENCY = "concurrency"
    TASK_QUEUE = "task_queue"
    VALIDATION = "validation"
    CLI = "cli"
    TESTING = "testing"


class StackComponent(Contract):
    key: str = Field(description='Import name, e.g. "sqlite3" or "concurrent.futures".')
    name: str = Field(description='Display name, e.g. "SQLite".')
    category: StackCategory
    declared: bool = Field(description="Listed in a dependency file.")
    used_in_app: bool = Field(description="Imported by non-test code.")
    used_in_tests: bool
    evidence: list[str] = Field(default=[], description='e.g. "orders/db.py:5 import sqlite3".')


class Layer(StrEnum):
    ENTRY = "entry"
    API = "api"
    SERVICE = "service"
    DATA = "data"
    UTIL = "util"
    TEST = "test"  # test modules are kept out of the application layers


class ModuleLayer(Contract):
    module: str
    path: str
    layer: Layer
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = []


class LayerDependency(Contract):
    source: Layer
    target: Layer
    imports: int = Field(description="Number of module-to-module imports between the two layers.")
    upward: bool = Field(description="True when a lower layer depends on a higher one, e.g. data -> api.")


class SystemAnalystInput(Contract):
    root: Path
    repo: RepoScoutOutput


class SystemAnalystOutput(Contract):
    python_requires: str | None = None
    dependency_files: list[str] = []
    dependencies: list[str] = Field(default=[], description="Normalized distribution names.")
    stack: list[StackComponent] = []
    test_runner: TestRunner | None = None
    test_paths: list[str] = []
    entrypoints: list[Entrypoint] = []
    layers: list[ModuleLayer] = []
    layer_dependencies: list[LayerDependency] = []
    architecture: ArchitectureStyle = ArchitectureStyle.UNKNOWN
    summary: str = ""

    def layer_of(self, module: str) -> Layer | None:
        return next((m.layer for m in self.layers if m.module == module), None)


# --- 3. PipelineArchitect ---------------------------------------------------


class NodeType(StrEnum):
    ENTRY = "entry"  # a __main__ block, route handler, CLI command or entry-layer function
    FUNCTION = "function"  # any other function or method in the repo
    SERVICE = "service"  # a function or method in the service layer
    DATABASE = "database"  # a datastore the app talks to, e.g. SQLite
    EXTERNAL = "external"  # a library doing I/O or concurrency, e.g. requests, concurrent.futures


class EdgeKind(StrEnum):
    CALL = "call"  # source calls target
    DATA = "data"  # source reads from or writes to a database node
    IMPORT = "import"  # static reference without a call here: a callback passed on, a type annotation


class NodeStatus(StrEnum):
    """Set by later stages as evidence arrives; PipelineArchitect leaves every node idle."""

    IDLE = "idle"
    HEALTHY = "healthy"
    WARNING = "warning"
    FAILING = "failing"
    PATCHED = "patched"
    FIXED = "fixed"  # RegressionCheck verified that an accepted patch resolves every finding on the node


class NodeMetrics(Contract):
    loc: int | None = None
    complexity: int | None = Field(default=None, description="Cyclomatic complexity (functions only).")
    fan_in: int = Field(default=0, description="Distinct graph nodes with an edge into this one.")
    fan_out: int = Field(default=0, description="Distinct graph nodes this one has an edge to.")
    call_sites: int = Field(default=0, description="Call sites into this node across the repo.")
    findings: int = Field(default=0, description="DiagnosticSentinel findings located in this node.")
    seen_before: int = Field(default=0, description="Most earlier runs in which one of this node's patterns was seen.")


class Position(Contract):
    x: float
    y: float


class GraphNode(Contract):
    id: str
    label: str
    type: NodeType
    file: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    layer: Layer | None = None
    status: NodeStatus = NodeStatus.IDLE
    metrics: NodeMetrics = NodeMetrics()
    position: Position = Field(description="Top-left corner; columns run left to right by layer.")
    symbol: str | None = Field(
        default=None, description="Qualname for function nodes, import name for database/external nodes."
    )


class GraphEdge(Contract):
    id: str
    source: str
    target: str
    kind: EdgeKind
    animated: bool = False


class GraphColumn(Contract):
    key: str = Field(description='Layer or node type shown in this column, e.g. "service" or "database".')
    title: str
    x: float


class Graph(Contract):
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    columns: list[GraphColumn] = Field(default=[], description="Non-empty columns, left to right.")


class PipelineArchitectInput(Contract):
    repo: RepoScoutOutput
    system: SystemAnalystOutput
    include_tests: bool = False


class PipelineArchitectOutput(Contract):
    graph: Graph


# --- 4. SandboxRunner -------------------------------------------------------


class Isolation(StrEnum):
    DOCKER = "docker"
    SUBPROCESS = "subprocess"
    NONE = "none"


class CaseResult(Contract):
    node_id: str = Field(description='Test id, e.g. "tests/test_api.py::test_create".')
    outcome: CaseOutcome
    duration_s: float = 0.0
    message: str | None = None
    exception: ExceptionInfo | None = None


class SyntheticProbe(Contract):
    target: str = Field(description="Qualname of the callable that was probed.")
    inputs: str = Field(description="repr() of the generated arguments.")
    outcome: CaseOutcome
    exception: ExceptionInfo | None = None


class SandboxRunnerInput(Contract):
    root: Path
    work_dir: Path = Field(description="The run's scratch directory: the sandbox copy and virtualenv live under it.")
    dependencies: list[str] = []
    test_runner: TestRunner | None
    test_paths: list[str] = []
    entrypoints: list[Entrypoint] = []
    graph: Graph | None = None


class SandboxRunnerOutput(Contract):
    isolation: Isolation
    tests: list[CaseResult] = []
    probes: list[SyntheticProbe] = []
    totals: SuiteTotals = SuiteTotals()
    duration_s: float = 0.0


# --- 5. DiagnosticSentinel --------------------------------------------------


class FindingCategory(StrEnum):
    EXCEPTION = "exception"
    RACE_CONDITION = "race_condition"
    SECURITY = "security"


class FindingSource(StrEnum):
    SANDBOX = "sandbox"  # an exception or traceback captured while running tests or probes
    BANDIT = "bandit"  # static security scan, mapped to OWASP Top 10
    STATIC = "static"  # CodeLoop's own AST checks (shared state touched by threads)


class Finding(Contract):
    id: str = Field(description="Stable across runs for the same rule and location.")
    category: FindingCategory
    source: FindingSource
    rule_id: str
    title: str
    severity: Severity
    location: SourceLocation
    node_id: str | None = Field(default=None, description="Graph node containing the location, when graphed.")
    evidence: str
    owasp: str | None = Field(default=None, description='OWASP Top 10 id, e.g. "A03:2021".')
    owasp_name: str | None = Field(default=None, description='e.g. "Injection".')
    exception: ExceptionInfo | None = None
    source_test: str | None = Field(default=None, description="Test or probe that surfaced it.")
    signature: str | None = Field(
        default=None,
        description="Hash of the exception type (or rule), the normalized code pattern and the OWASP category. "
        "None for findings in test code, which are not remembered.",
    )
    pattern: str | None = Field(default=None, description="The normalized code pattern behind the signature.")
    seen_before: int = Field(default=0, description="In how many earlier runs this signature was already recorded.")
    base_severity: Severity | None = Field(
        default=None, description="The severity before it was raised for a pattern seen before."
    )


class SourceStatus(StrEnum):
    OK = "ok"
    SKIPPED = "skipped"
    FAILED = "failed"


class SourceReport(Contract):
    source: FindingSource
    status: SourceStatus
    findings: int = 0
    detail: str | None = None


class DiagnosticSentinelInput(Contract):
    root: Path
    repo: RepoScoutOutput
    sandbox: SandboxRunnerOutput | None = None
    graph: Graph | None = None


class DiagnosticSentinelOutput(Contract):
    findings: list[Finding] = []
    sources: list[SourceReport] = Field(default=[], description="What each collector did, even when it found nothing.")


# --- 6. RootCauseDiagnostician ----------------------------------------------


class ChainRole(StrEnum):
    ENTRY = "entry"  # where execution enters the app
    PATH = "path"  # on the way
    OFFENDER = "offender"  # contains the offending file and line


class ChainStep(Contract):
    node_id: str
    label: str
    file: str | None = None
    line: int | None = Field(default=None, description="Call site for path steps; the offending line for the offender.")
    role: ChainRole
    via: str = Field(default="graph", description='"traceback" when taken from an actual stack, else "graph".')


class CodeSnippet(Contract):
    file: str
    start_line: int
    highlight_line: int
    code: str = Field(description="Raw lines from start_line, joined with newlines.")


class ExplanationSource(StrEnum):
    CLAUDE = "claude"
    TEMPLATE = "template"  # deterministic fallback: no credentials, a refusal, or an API error


class RootCause(Contract):
    id: str
    finding_ids: list[str] = Field(min_length=1)
    location: SourceLocation
    node_id: str | None = None
    symbol: str | None = None
    chain: list[ChainStep] = Field(default=[], description="Entry node first, offender last.")
    snippet: CodeSnippet | None = None
    explanation: str
    explanation_source: ExplanationSource = ExplanationSource.TEMPLATE
    confidence: float = Field(ge=0.0, le=1.0)
    evidence: list[str] = []


class RootCauseDiagnosticianInput(Contract):
    root: Path
    repo: RepoScoutOutput
    findings: list[Finding]
    graph: Graph | None = None


class RootCauseDiagnosticianOutput(Contract):
    root_causes: list[RootCause] = []


# --- 7. PatchMaster ---------------------------------------------------------


class PatchStatus(StrEnum):
    VALID = "valid"  # the diff passed `git apply --check` and the patched files still parse
    INVALID = "invalid"  # still failing after the retry; `detail` has git's last error
    SKIPPED = "skipped"  # no patch was attempted or Claude produced none; `detail` says why


class PatchedFile(Contract):
    path: str = Field(description="Relative to the repo root, POSIX separators.")
    original: str
    patched: str


class Patch(Contract):
    id: str
    root_cause_id: str
    status: PatchStatus
    detail: str | None = Field(default=None, description="Why a patch is invalid or skipped.")
    diff: str = Field(default="", description="Unified diff, paths relative to the repo root.")
    files: list[PatchedFile] = Field(default=[], description="Before and after text, for side-by-side review.")
    design_suggestion: str = Field(default="", description="One paragraph on a hardened design, beyond the minimal fix.")
    attempts: int = Field(default=0, description="Requests made to Claude: 1, or 2 when the first diff did not apply.")
    rationale: str = ""
    memory_examples: int = Field(default=0, description="Past accepted fixes for similar incidents shown to Claude.")


class PatchMasterInput(Contract):
    root: Path
    repo: RepoScoutOutput
    findings: list[Finding] = []
    root_causes: list[RootCause]


class PatchMasterOutput(Contract):
    patches: list[Patch] = []


# --- 8. RegressionCheck -----------------------------------------------------


class Verdict(StrEnum):
    PASS = "pass"  # applies, breaks nothing, introduces no finding
    REGRESSED = "regressed"  # a test that passed now fails, or a new finding appeared
    APPLY_FAILED = "apply_failed"  # does not apply to a fresh copy, alone or on top of the accepted patches


class FindingRef(Contract):
    """A finding named in a comparison; the full finding lives in DiagnosticSentinel's output."""

    finding_id: str
    rule_id: str
    title: str
    severity: Severity
    location: SourceLocation
    node_id: str | None = None


class PatchVerdict(Contract):
    patch_id: str
    root_cause_id: str = ""
    verdict: Verdict
    accepted: bool
    reasons: list[str] = Field(default=[], description="Why a patch was rejected, or notes on an accepted one.")
    totals: SuiteTotals = SuiteTotals()
    newly_failing: list[str] = Field(default=[], description="Tests that passed before and fail with this patch alone.")
    newly_passing: list[str] = Field(default=[], description="Tests that failed before and pass with this patch alone.")
    findings_resolved: list[FindingRef] = []
    findings_introduced: list[FindingRef] = []
    detail: str | None = None


class RegressionSummary(Contract):
    """Before and after for the accepted patches applied together to a fresh copy."""

    patches_applied: list[str] = []
    tests_before: SuiteTotals = SuiteTotals()
    tests_after: SuiteTotals = SuiteTotals()
    tests_fixed: list[str] = []
    tests_broken: list[str] = []
    findings_before: int = 0
    findings_after: int = 0
    findings_resolved: list[FindingRef] = []
    findings_introduced: list[FindingRef] = []


class RegressionCheckInput(Contract):
    root: Path
    work_dir: Path
    repo: RepoScoutOutput
    patches: list[Patch]
    findings: list[Finding] | None = Field(
        default=None, description="DiagnosticSentinel's findings: the 'before' side. None when it did not run."
    )
    dependencies: list[str] = []
    test_runner: TestRunner | None
    test_paths: list[str] = []
    baseline: SandboxRunnerOutput | None = None


class RegressionCheckOutput(Contract):
    baseline_totals: SuiteTotals | None = None
    verdicts: list[PatchVerdict] = []
    summary: RegressionSummary | None = None


# --- 9. MemoryKeeper --------------------------------------------------------


class IncidentRecord(Contract):
    signature: str = Field(description="Hash of the exception type, the normalized code pattern and the OWASP category.")
    finding_id: str
    root_cause_id: str | None = None
    patch_id: str | None = None
    regression_passed: bool | None = Field(default=None, description="None when there was no patch to verify.")
    is_new: bool = Field(description="No earlier run had recorded this signature.")
    occurrences: int = Field(ge=1, description="Incidents with this signature in the memory, including this one.")


class MemoryKeeperInput(Contract):
    run_id: str
    source: str
    findings: list[Finding]
    root_causes: list[RootCause] = []
    patches: list[Patch] = []
    verdicts: list[PatchVerdict] = []


class MemoryKeeperOutput(Contract):
    incidents: list[IncidentRecord] = []

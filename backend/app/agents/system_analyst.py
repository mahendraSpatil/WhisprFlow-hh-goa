from __future__ import annotations

import asyncio
import configparser
import re
import tomllib
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.base import Agent
from app.models.agents import (
    ArchitectureStyle,
    CallResolution,
    Entrypoint,
    EntrypointKind,
    ImportRef,
    Layer,
    LayerDependency,
    ModuleInfo,
    ModuleLayer,
    RepoScoutOutput,
    SourceLocation,
    StackCategory,
    StackComponent,
    SystemAnalystInput,
    SystemAnalystOutput,
    TestRunner,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger


@dataclass(frozen=True)
class Tech:
    name: str
    category: StackCategory
    dists: frozenset[str] = frozenset()  # distribution names; empty for the stdlib


def _t(name: str, category: StackCategory, *dists: str) -> Tech:
    return Tech(name, category, frozenset(dists))


C = StackCategory
# Keyed by import name; dotted keys match that module and its submodules.
KNOWN_TECH: dict[str, Tech] = {
    "flask": _t("Flask", C.WEB_FRAMEWORK, "flask"),
    "fastapi": _t("FastAPI", C.WEB_FRAMEWORK, "fastapi"),
    "django": _t("Django", C.WEB_FRAMEWORK, "django"),
    "starlette": _t("Starlette", C.WEB_FRAMEWORK, "starlette"),
    "aiohttp": _t("aiohttp", C.WEB_FRAMEWORK, "aiohttp"),
    "tornado": _t("Tornado", C.WEB_FRAMEWORK, "tornado"),
    "sanic": _t("Sanic", C.WEB_FRAMEWORK, "sanic"),
    "bottle": _t("Bottle", C.WEB_FRAMEWORK, "bottle"),
    "falcon": _t("Falcon", C.WEB_FRAMEWORK, "falcon"),
    "wsgiref": _t("wsgiref (stdlib WSGI)", C.WEB_SERVER),
    "http.server": _t("http.server (stdlib)", C.WEB_SERVER),
    "uvicorn": _t("Uvicorn", C.WEB_SERVER, "uvicorn"),
    "gunicorn": _t("Gunicorn", C.WEB_SERVER, "gunicorn"),
    "waitress": _t("Waitress", C.WEB_SERVER, "waitress"),
    "requests": _t("Requests", C.HTTP_CLIENT, "requests"),
    "httpx": _t("HTTPX", C.HTTP_CLIENT, "httpx"),
    "urllib3": _t("urllib3", C.HTTP_CLIENT, "urllib3"),
    "urllib.request": _t("urllib.request (stdlib)", C.HTTP_CLIENT),
    "http.client": _t("http.client (stdlib)", C.HTTP_CLIENT),
    "sqlite3": _t("SQLite", C.DATABASE),
    "psycopg2": _t("PostgreSQL (psycopg2)", C.DATABASE, "psycopg2", "psycopg2-binary"),
    "psycopg": _t("PostgreSQL (psycopg)", C.DATABASE, "psycopg", "psycopg-binary"),
    "asyncpg": _t("PostgreSQL (asyncpg)", C.DATABASE, "asyncpg"),
    "pymysql": _t("MySQL (PyMySQL)", C.DATABASE, "pymysql"),
    "mysql.connector": _t("MySQL Connector", C.DATABASE, "mysql-connector-python"),
    "pymongo": _t("MongoDB", C.DATABASE, "pymongo"),
    "sqlalchemy": _t("SQLAlchemy", C.ORM, "sqlalchemy"),
    "peewee": _t("Peewee", C.ORM, "peewee"),
    "tortoise": _t("Tortoise ORM", C.ORM, "tortoise-orm"),
    "redis": _t("Redis", C.CACHE, "redis"),
    "threading": _t("threading", C.CONCURRENCY),
    "concurrent.futures": _t("concurrent.futures", C.CONCURRENCY),
    "multiprocessing": _t("multiprocessing", C.CONCURRENCY),
    "asyncio": _t("asyncio", C.CONCURRENCY),
    "celery": _t("Celery", C.TASK_QUEUE, "celery"),
    "rq": _t("RQ", C.TASK_QUEUE, "rq"),
    "dramatiq": _t("Dramatiq", C.TASK_QUEUE, "dramatiq"),
    "pydantic": _t("Pydantic", C.VALIDATION, "pydantic"),
    "marshmallow": _t("marshmallow", C.VALIDATION, "marshmallow"),
    "click": _t("Click", C.CLI, "click"),
    "typer": _t("Typer", C.CLI, "typer"),
    "argparse": _t("argparse", C.CLI),
    "pytest": _t("pytest", C.TESTING, "pytest"),
    "unittest": _t("unittest", C.TESTING),
    "hypothesis": _t("Hypothesis", C.TESTING, "hypothesis"),
}
WEB_CATEGORIES = {C.WEB_FRAMEWORK, C.WEB_SERVER}
DATA_CATEGORIES = {C.DATABASE, C.ORM, C.CACHE}

DEPENDENCY_FILES = ("pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "poetry.lock")
ROUTE_DECORATOR = re.compile(r"^[\w.]+\.(get|post|put|patch|delete|head|options|route|api_route|websocket)\(")
CLI_DECORATOR = re.compile(r"(^|\.)(command|group)$")  # matched against the decorator minus its call args
DIST_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
DATA_CALLS = re.compile(r"\.(execute|executemany|executescript|cursor|commit|rollback|query|add|flush)$")
MAX_EVIDENCE = 5

LAYER_NAMES: dict[Layer, frozenset[str]] = {
    Layer.ENTRY: frozenset({"main", "__main__", "cli", "manage", "run", "wsgi", "asgi", "entrypoint", "entrypoints"}),
    Layer.API: frozenset({
        "api", "apis", "routes", "router", "routers", "views", "endpoints", "handlers", "controllers",
        "resources", "web", "server", "http", "rest", "graphql",
    }),
    Layer.SERVICE: frozenset({
        "service", "services", "domain", "logic", "core", "business", "usecases", "use_cases",
        "workflows", "manager", "managers", "tasks", "jobs",
    }),
    Layer.DATA: frozenset({
        "db", "database", "models", "model", "repository", "repositories", "repo", "dao", "store",
        "storage", "schema", "schemas", "persistence", "migrations", "orm", "tables", "queries",
    }),
    Layer.UTIL: frozenset({
        "util", "utils", "helpers", "helper", "common", "lib", "tools", "config", "settings",
        "constants", "shared", "types", "exceptions", "errors",
    }),
}
# Lower rank = closer to the user. A module should only import modules of equal or higher rank.
LAYER_RANK = {Layer.ENTRY: 0, Layer.API: 1, Layer.SERVICE: 2, Layer.DATA: 3, Layer.UTIL: 4}
APP_LAYER_ORDER = [Layer.ENTRY, Layer.API, Layer.SERVICE, Layer.DATA]


class SystemAnalyst(Agent[SystemAnalystInput, SystemAnalystOutput]):
    """Detects the stack, classifies modules into layers and summarizes the architecture."""

    name = StageName.SYSTEM_ANALYST
    title = "SystemAnalyst"
    output_model = SystemAnalystOutput
    requires = (StageName.REPO_SCOUT,)
    default_timeout_s = 30.0

    def build_input(self, ctx: RunContext) -> SystemAnalystInput:
        return SystemAnalystInput(root=ctx.require_workspace(), repo=ctx.require(StageName.REPO_SCOUT, RepoScoutOutput))

    async def run(self, inp: SystemAnalystInput, log: StageLogger) -> SystemAnalystOutput:
        out = await asyncio.to_thread(analyze, inp)
        log.info(out.summary)
        for dep in out.layer_dependencies:
            if dep.upward:
                log.warning(f"Upward dependency: {dep.source} -> {dep.target} ({dep.imports} imports)")
        return out


def analyze(inp: SystemAnalystInput) -> SystemAnalystOutput:
    repo = inp.repo
    declared = read_dependencies(inp.root)
    stack = detect_stack(repo, declared.dependencies)
    entrypoints = find_entrypoints(repo)
    layers = classify_layers(repo, stack, entrypoints)
    layer_deps = layer_dependencies(repo, layers)
    test_runner, test_paths = detect_tests(repo, declared, stack)
    architecture = classify_architecture(stack, entrypoints, bool(repo.modules))
    out = SystemAnalystOutput(
        python_requires=declared.python_requires,
        dependency_files=declared.files,
        dependencies=sorted(declared.dependencies),
        stack=stack,
        test_runner=test_runner,
        test_paths=test_paths,
        entrypoints=entrypoints,
        layers=layers,
        layer_dependencies=layer_deps,
        architecture=architecture,
    )
    out.summary = summarize(out, repo)
    return out


# --- Declared dependencies ----------------------------------------------------


@dataclass
class Declared:
    files: list[str] = field(default_factory=list)
    dependencies: dict[str, str] = field(default_factory=dict)  # dist name -> "file: spec"
    python_requires: str | None = None
    pytest_configured: bool = False

    def add(self, spec: str, source: str) -> None:
        if name := _dist_name(spec):
            self.dependencies.setdefault(name, f"{source}: {spec.strip()}")


def read_dependencies(root: Path) -> Declared:
    declared = Declared(files=[name for name in DEPENDENCY_FILES if (root / name).is_file()])
    declared.pytest_configured = (root / "pytest.ini").is_file() or (root / "conftest.py").is_file()

    if pyproject := _load_toml(root / "pyproject.toml"):
        project = pyproject.get("project", {})
        declared.python_requires = project.get("requires-python")
        for spec in project.get("dependencies", []):
            declared.add(spec, "pyproject.toml")
        for extra in project.get("optional-dependencies", {}).values():
            for spec in extra:
                declared.add(spec, "pyproject.toml")
        poetry = pyproject.get("tool", {}).get("poetry", {})
        poetry_deps = dict(poetry.get("dependencies", {}))
        for group in poetry.get("group", {}).values():
            poetry_deps.update(group.get("dependencies", {}))
        python = poetry_deps.pop("python", None)
        declared.python_requires = declared.python_requires or (python if isinstance(python, str) else None)
        for name, version in poetry_deps.items():
            declared.add(f"{name} {version if isinstance(version, str) else ''}", "pyproject.toml")
        declared.pytest_configured |= "pytest" in pyproject.get("tool", {})

    for req in sorted(root.glob("requirements*.txt")):
        declared.files.append(req.name)
        for line in req.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.split("#", 1)[0].strip()
            if line and not line.startswith("-"):
                declared.add(line, req.name)

    if (setup_cfg := root / "setup.cfg").is_file():
        parser = configparser.ConfigParser()
        try:
            parser.read(setup_cfg, encoding="utf-8")
        except configparser.Error:
            parser = configparser.ConfigParser()
        for spec in parser.get("options", "install_requires", fallback="").splitlines():
            declared.add(spec, "setup.cfg")
        declared.pytest_configured |= parser.has_section("tool:pytest")

    if pipfile := _load_toml(root / "Pipfile"):
        for section in ("packages", "dev-packages"):
            for name in pipfile.get(section, {}):
                declared.add(name, "Pipfile")
    return declared


def _load_toml(path: Path) -> dict[str, Any] | None:
    if not path.is_file():
        return None
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError):
        return None


def _dist_name(spec: str) -> str | None:
    match = DIST_NAME.match(spec)
    return match.group(1).lower().replace("_", "-") if match else None


# --- Stack --------------------------------------------------------------------


def tech_key(imported: str) -> str | None:
    parts = imported.split(".")
    for i in range(len(parts), 0, -1):
        if (key := ".".join(parts[:i])) in KNOWN_TECH:
            return key
    return None


def _import_tech_keys(imp: ImportRef) -> set[str]:
    """Known tech an import pulls in; ``from http import server`` counts as http.server."""
    if imp.level or not imp.module:
        return set()
    candidates = [imp.module, *(f"{imp.module}.{n}" for n in imp.names)]
    return {k for k in map(tech_key, candidates) if k}


def detect_stack(repo: RepoScoutOutput, declared: dict[str, str]) -> list[StackComponent]:
    internal_tops = {m.name.split(".")[0] for m in repo.modules}
    found: dict[str, dict[str, Any]] = {}

    def entry(key: str) -> dict[str, Any]:
        return found.setdefault(key, {"declared": False, "app": False, "tests": False, "evidence": []})

    for key, tech in KNOWN_TECH.items():
        for dist in tech.dists:
            if dist in declared:
                e = entry(key)
                e["declared"] = True
                e["evidence"].append(declared[dist])

    for m in repo.modules:
        for imp in m.imports:
            if imp.module.split(".")[0] in internal_tops:
                continue
            for key in _import_tech_keys(imp):
                e = entry(key)
                e["tests" if m.is_test else "app"] = True
                if len(e["evidence"]) < MAX_EVIDENCE:
                    e["evidence"].append(f"{m.path}:{imp.line} imports {imp.module}")

    return [
        StackComponent(
            key=key,
            name=KNOWN_TECH[key].name,
            category=KNOWN_TECH[key].category,
            declared=e["declared"],
            used_in_app=e["app"],
            used_in_tests=e["tests"],
            evidence=e["evidence"],
        )
        for key, e in sorted(found.items(), key=lambda kv: (KNOWN_TECH[kv[0]].category, kv[0]))
    ]


# --- Entrypoints ----------------------------------------------------------------


def find_entrypoints(repo: RepoScoutOutput) -> list[Entrypoint]:
    test_modules = {m.name for m in repo.modules if m.is_test}
    found: list[Entrypoint] = []
    for m in repo.modules:
        if m.main_guard_line and m.name not in test_modules:
            found.append(
                Entrypoint(
                    kind=EntrypointKind.MAIN_GUARD,
                    module=m.name,
                    location=SourceLocation(file=m.path, line=m.main_guard_line),
                )
            )
    for s in repo.symbols:
        if s.module in test_modules:
            continue
        for dec in s.decorators:
            kind = None
            if ROUTE_DECORATOR.match(dec):
                kind = EntrypointKind.WEB_ROUTE
            elif CLI_DECORATOR.search(dec.split("(", 1)[0]):
                kind = EntrypointKind.CLI_COMMAND
            if kind:
                found.append(Entrypoint(kind=kind, module=s.module, symbol=s.qualname, location=s.location, detail=dec))
                break
    return found


# --- Layers -----------------------------------------------------------------------


class _Scores:
    def __init__(self) -> None:
        self.points: Counter[Layer] = Counter()
        self.evidence: dict[Layer, list[str]] = defaultdict(list)

    def add(self, layer: Layer, points: int, why: str) -> None:
        self.points[layer] += points
        self.evidence[layer].append(why)

    def best(self) -> tuple[Layer, float]:
        if not self.points:
            return Layer.UTIL, 0.3
        # Ties go to the earlier layer in LAYER_RANK, i.e. the more user-facing role.
        layer = max(self.points, key=lambda l: (self.points[l], -LAYER_RANK[l]))
        return layer, round(self.points[layer] / sum(self.points.values()), 2)


def classify_layers(
    repo: RepoScoutOutput, stack: list[StackComponent], entrypoints: list[Entrypoint]
) -> list[ModuleLayer]:
    categories = {c.key: c.category for c in stack}
    app_modules = [m for m in repo.modules if not m.is_test]
    symbols_by_module: dict[str, int] = Counter(s.module for s in repo.symbols)
    data_calls: Counter[str] = Counter()
    module_of_path = {m.path: m.name for m in repo.modules}
    for call in repo.calls:
        if (
            call.resolution is CallResolution.EXTERNAL
            and call.target
            and (key := tech_key(call.target))
            and categories.get(key) in DATA_CATEGORIES
            and DATA_CALLS.search(call.target)
        ):
            data_calls[module_of_path.get(call.location.file, "")] += 1

    scores: dict[str, _Scores] = {}
    for m in app_modules:
        s = scores[m.name] = _Scores()
        _score_names(m, s)
        if m.main_guard_line:
            s.add(Layer.ENTRY, 3, f"has `if __name__ == '__main__'` at line {m.main_guard_line}")
        for e in entrypoints:
            if e.module == m.name and e.kind is EntrypointKind.WEB_ROUTE:
                s.add(Layer.API, 4, f"route {e.detail}")
            elif e.module == m.name and e.kind is EntrypointKind.CLI_COMMAND:
                s.add(Layer.ENTRY, 2, f"CLI command {e.detail}")
        for imp in m.imports:
            for key in _import_tech_keys(imp):
                category = categories.get(key)
                if category in WEB_CATEGORIES:
                    s.add(Layer.API, 3, f"imports {key} ({category})")
                elif category in DATA_CATEGORIES:
                    s.add(Layer.DATA, 3, f"imports {key} ({category})")
        if data_calls[m.name]:
            s.add(Layer.DATA, 2, f"{data_calls[m.name]} database calls")
        if not m.imports and not symbols_by_module[m.name]:
            s.add(Layer.UTIL, 1, "empty module (package marker)")

    # Structural pass: where a module sits in the internal import graph.
    tentative = {name: s.best()[0] for name, s in scores.items()}
    importers: dict[str, set[str]] = defaultdict(set)
    for m in app_modules:
        for dep in m.depends_on:
            importers[dep].add(m.name)
    for m in app_modules:
        s = scores[m.name]
        app_deps = [d for d in m.depends_on if d in tentative]
        imported_by = {tentative[i] for i in importers[m.name] if i in tentative}
        if tentative[m.name] not in (Layer.API, Layer.DATA, Layer.ENTRY):
            if any(tentative[d] is Layer.DATA for d in app_deps):
                s.add(Layer.SERVICE, 2, "imports the data layer")
            if imported_by & {Layer.API, Layer.ENTRY}:
                s.add(Layer.SERVICE, 2, "used by the api/entry layer")
        if not app_deps and len(importers[m.name]) >= 2 and tentative[m.name] is Layer.UTIL:
            s.add(Layer.UTIL, 2, f"imported by {len(importers[m.name])} modules, imports none")

    result = []
    for m in repo.modules:
        if m.is_test:
            result.append(ModuleLayer(module=m.name, path=m.path, layer=Layer.TEST, confidence=1.0,
                                      evidence=["test module"]))
            continue
        layer, confidence = scores[m.name].best()
        evidence = scores[m.name].evidence.get(layer) or ["no strong signal; defaulted to util"]
        result.append(ModuleLayer(module=m.name, path=m.path, layer=layer, confidence=confidence, evidence=evidence))
    return result


def _score_names(m: ModuleInfo, s: _Scores) -> None:
    parts = m.name.split(".")
    for i, part in enumerate(parts):
        weight = 3 if i == len(parts) - 1 else 2
        for layer, names in LAYER_NAMES.items():
            if part.lower() in names:
                s.add(layer, weight, f"named `{part}`")


def layer_dependencies(repo: RepoScoutOutput, layers: list[ModuleLayer]) -> list[LayerDependency]:
    layer_of = {l.module: l.layer for l in layers}
    counts: Counter[tuple[Layer, Layer]] = Counter()
    for m in repo.modules:
        source = layer_of.get(m.name)
        if source is None or source is Layer.TEST:
            continue
        for dep in m.depends_on:
            target = layer_of.get(dep)
            if target is not None and target is not Layer.TEST and target is not source:
                counts[(source, target)] += 1
    return [
        LayerDependency(source=s, target=t, imports=n, upward=LAYER_RANK[s] > LAYER_RANK[t])
        for (s, t), n in sorted(counts.items(), key=lambda kv: (LAYER_RANK[kv[0][0]], LAYER_RANK[kv[0][1]]))
    ]


# --- Tests, style, summary ------------------------------------------------------


def detect_tests(
    repo: RepoScoutOutput, declared: Declared, stack: list[StackComponent]
) -> tuple[TestRunner | None, list[str]]:
    keys = {c.key for c in stack}
    test_modules = [m for m in repo.modules if m.is_test]
    test_paths = sorted({m.path.rsplit("/", 1)[0] if "/" in m.path else "." for m in test_modules})
    if declared.pytest_configured or "pytest" in keys:
        return "pytest", test_paths
    if test_modules and "unittest" in keys:
        return "unittest", test_paths
    if test_modules:
        return "pytest", test_paths  # pytest also collects plain and unittest-style tests
    return None, test_paths


def classify_architecture(
    stack: list[StackComponent], entrypoints: list[Entrypoint], has_code: bool
) -> ArchitectureStyle:
    app_categories = {c.category for c in stack if c.used_in_app or (c.declared and not c.used_in_tests)}
    kinds = {e.kind for e in entrypoints}
    if app_categories & WEB_CATEGORIES or EntrypointKind.WEB_ROUTE in kinds:
        return ArchitectureStyle.WEB_SERVICE
    if C.TASK_QUEUE in app_categories:
        return ArchitectureStyle.WORKER
    if C.CLI in app_categories or kinds & {EntrypointKind.CLI_COMMAND, EntrypointKind.MAIN_GUARD}:
        return ArchitectureStyle.CLI
    return ArchitectureStyle.LIBRARY if has_code else ArchitectureStyle.UNKNOWN


STYLE_LABELS = {
    ArchitectureStyle.WEB_SERVICE: "Web service",
    ArchitectureStyle.WORKER: "Background worker",
    ArchitectureStyle.CLI: "Command-line app",
    ArchitectureStyle.LIBRARY: "Library",
    ArchitectureStyle.UNKNOWN: "Repository",
}


def summarize(out: SystemAnalystOutput, repo: RepoScoutOutput) -> str:
    app_stack = [c for c in out.stack if c.used_in_app]

    def names(*categories: StackCategory) -> str:
        return ", ".join(c.name for c in app_stack if c.category in categories)

    first = STYLE_LABELS[out.architecture]
    if web := names(C.WEB_FRAMEWORK, C.WEB_SERVER):
        first += f" on {web}"
    if data := names(C.DATABASE, C.ORM, C.CACHE):
        first += f" with {data} storage"
    sentences = [first + "."]

    app_layers = [l for l in out.layers if l.layer is not Layer.TEST]
    present = [layer for layer in APP_LAYER_ORDER if any(l.layer is layer for l in app_layers)]
    utils = sum(l.layer is Layer.UTIL for l in app_layers)
    if present:
        layer_text = f"{len(app_layers)} application modules layered " + " -> ".join(present)
        sentences.append(layer_text + (f" (+{utils} util)." if utils else "."))
    else:
        sentences.append(f"{len(app_layers)} modules, none in an entry/api/service/data layer.")

    upward = [d for d in out.layer_dependencies if d.upward]
    if upward:
        sentences.append("Upward dependencies: " + ", ".join(f"{d.source} -> {d.target}" for d in upward) + ".")
    if concurrency := names(C.CONCURRENCY):
        sentences.append(f"Concurrency: {concurrency}.")
    if clients := names(C.HTTP_CLIENT):
        sentences.append(f"Outbound HTTP: {clients}.")
    test_count = sum(m.is_test for m in repo.modules)
    if out.test_runner:
        sentences.append(f"Tests: {out.test_runner}, {test_count} test modules.")
    else:
        sentences.append("No tests found.")
    return " ".join(sentences)

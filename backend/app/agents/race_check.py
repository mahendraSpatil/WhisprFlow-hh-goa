"""Static race-condition check: shared state modified by threaded code without a lock.

1. Thread entry points: callables handed to ``threading.Thread(target=...)``, thread-pool
   ``submit``/``map``, ``asyncio.to_thread`` and the like, plus ``Thread.run`` overrides.
2. Everything reachable from an entry through the resolved call graph runs on a worker
   thread. A call made inside ``with <lock>:`` is not followed: the callee runs protected.
3. In that code, report modifications of state other threads can see, outside any lock:
   module globals (``global x``, module-level containers), instance attributes (methods
   only, ``__init__`` excluded) and ``nonlocal`` closure variables.

It is a heuristic. It cannot tell whether two threads really share one instance, so
instance findings are rated lower unless the write is a read-modify-write (an augmented
assignment, or the same state read earlier in the function: check-then-act).
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict, deque
from dataclasses import dataclass, field
from pathlib import Path

from app.agents.indexer import module_name
from app.models.agents import CallResolution, RepoScoutOutput, Severity, SymbolKind

# resolved call target -> (keyword name of the callable, positional index of the callable)
SPAWNERS: dict[str, tuple[str | None, int]] = {
    "threading.Thread": ("target", 1),
    "threading.Timer": ("function", 1),
    "concurrent.futures.ThreadPoolExecutor.submit": (None, 0),
    "concurrent.futures.ThreadPoolExecutor.map": (None, 0),
    "asyncio.to_thread": (None, 0),
    "multiprocessing.pool.ThreadPool.map": (None, 0),
    "multiprocessing.pool.ThreadPool.imap": (None, 0),
    "multiprocessing.pool.ThreadPool.imap_unordered": (None, 0),
    "multiprocessing.pool.ThreadPool.starmap": (None, 0),
    "multiprocessing.pool.ThreadPool.apply": (None, 0),
    "multiprocessing.pool.ThreadPool.apply_async": (None, 0),
}

MUTATORS = frozenset({
    "append", "extend", "insert", "remove", "pop", "popitem", "clear", "update", "add", "discard",
    "setdefault", "sort", "reverse", "appendleft", "popleft", "extendleft", "difference_update",
    "intersection_update", "symmetric_difference_update",
})
LOCK_CTORS = frozenset({"Lock", "RLock", "Semaphore", "BoundedSemaphore", "Condition"})
CONTAINER_CTORS = frozenset({"dict", "list", "set", "defaultdict", "OrderedDict", "Counter", "deque", "bytearray"})
LOCK_NAME = re.compile(r"(?i)(^|[._])(r?lock|mutex|sem(aphore)?|cond(ition)?)\d*($|[._(\[])")
MAX_PATH_SHOWN = 6


@dataclass(frozen=True)
class RaceIssue:
    function: str  # qualname of the function containing the unprotected write
    file: str
    line: int
    scope: str  # "global" | "instance" | "closure"
    access: str  # "rebind" | "attr" | "item" | "call"
    key: str  # e.g. "self._stock", "COUNTER"
    read_modify_write: bool
    spawn: str  # how the code ends up on a thread, e.g. "OrderService.place_batch -> ThreadPoolExecutor.map(try_reserve)"
    path: tuple[str, ...]  # qualnames from the thread entry down to `function`

    @property
    def rule_id(self) -> str:
        if self.scope == "instance":
            return "RACE003"
        if self.scope == "closure":
            return "RACE004"
        return "RACE002" if self.access in ("item", "call") else "RACE001"

    @property
    def severity(self) -> Severity:
        if self.read_modify_write:
            return Severity.HIGH
        return Severity.LOW if self.scope == "instance" else Severity.MEDIUM

    @property
    def title(self) -> str:
        what = {"global": "module-level state", "instance": "instance state", "closure": "closure variable"}[self.scope]
        verb = "read-modify-write of" if self.read_modify_write else "write to"
        return f"Unsynchronized {verb} shared {what} `{self.key}`"


# --- Collection ---------------------------------------------------------------------


@dataclass
class _Func:
    qualname: str
    module: str
    path: str
    node: ast.FunctionDef | ast.AsyncFunctionDef
    class_qualname: str | None
    self_name: str | None
    is_test: bool


@dataclass
class _ModuleFacts:
    names: set[str] = field(default_factory=set)  # assigned at module level
    containers: set[str] = field(default_factory=set)


def _walk(node: ast.AST):
    """Descendants of a function body, without entering nested functions, classes or lambdas."""
    stack = list(ast.iter_child_nodes(node))
    while stack:
        n = stack.pop()
        yield n
        if not isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
            stack.extend(ast.iter_child_nodes(n))


def _dotted(node: ast.expr) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _call_name(node: ast.expr | None) -> str:
    return (_dotted(node.func) or "") if isinstance(node, ast.Call) else ""


def _is_container(value: ast.expr | None) -> bool:
    if isinstance(value, (ast.List, ast.Dict, ast.Set, ast.ListComp, ast.DictComp, ast.SetComp)):
        return True
    return _call_name(value).rsplit(".", 1)[-1] in CONTAINER_CTORS


def _flatten(target: ast.expr):
    if isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            yield from _flatten(elt)
    elif isinstance(target, ast.Starred):
        yield from _flatten(target.value)
    else:
        yield target


def _self_attr(expr: ast.expr, self_name: str | None) -> str | None:
    """For ``self.a`` / ``self.a.b`` / ``self.a[k]`` return "a": the instance attribute being touched."""
    if self_name is None:
        return None
    while isinstance(expr, (ast.Attribute, ast.Subscript)):
        inner = expr.value
        if isinstance(expr, ast.Attribute) and isinstance(inner, ast.Name):
            return expr.attr if inner.id == self_name else None
        expr = inner
    return None


class _Collector(ast.NodeVisitor):
    def __init__(self, module: str, path: str, is_test: bool) -> None:
        self.module, self.path, self.is_test = module, path, is_test
        self.funcs: dict[str, _Func] = {}
        self.classes: set[str] = set()
        self.calls: dict[tuple[int, int], ast.Call] = {}
        self._stack: list[tuple[str, bool]] = []  # (name, is_class)

    def _qual(self, name: str) -> str:
        return ".".join([self.module, *(n for n, _ in self._stack), name])

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.classes.add(self._qual(node.name))
        self._stack.append((node.name, True))
        self.generic_visit(node)
        self._stack.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        owner = self._qual("")[:-1] if self._stack and self._stack[-1][1] else None
        decorators = {_dotted(d) for d in node.decorator_list}
        params = [*node.args.posonlyargs, *node.args.args]
        self_name = params[0].arg if owner and params and "staticmethod" not in decorators else None
        qual = self._qual(node.name)
        self.funcs[qual] = _Func(qual, self.module, self.path, node, owner, self_name, self.is_test)
        self._stack.append((node.name, False))
        self.generic_visit(node)
        self._stack.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Call(self, node: ast.Call) -> None:
        self.calls[(node.lineno, node.col_offset)] = node
        self.generic_visit(node)


@dataclass
class _Facts:
    lock_regions: list[tuple[int, int]]
    uses_acquire: bool
    mutations: list[tuple[str, str, str, int, bool]]  # scope, access, key, line, read-modify-write


def _is_lock_expr(expr: ast.expr, lock_names: set[str]) -> bool:
    if isinstance(expr, ast.Call) and _call_name(expr).rsplit(".", 1)[-1] in LOCK_CTORS:
        return False  # `with threading.Lock():` builds a fresh lock each time and protects nothing
    text = ast.unparse(expr)
    return text in lock_names or bool(LOCK_NAME.search(text))


def _in_regions(line: int, regions: list[tuple[int, int]]) -> bool:
    return any(start <= line <= end for start, end in regions)


def _function_facts(
    f: _Func, mod: _ModuleFacts, class_containers: dict[str, set[str]], lock_names: set[str], tls_names: set[str]
) -> _Facts:
    node = f.node
    declared_global: set[str] = set()
    declared_nonlocal: set[str] = set()
    local: set[str] = {a.arg for a in [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs] if a}
    local.update(a.arg for a in (node.args.vararg, node.args.kwarg) if a)
    regions: list[tuple[int, int]] = []
    uses_acquire = False
    loads: list[tuple[str, int]] = []
    body = list(_walk(node))

    for n in body:
        if isinstance(n, ast.Global):
            declared_global.update(n.names)
        elif isinstance(n, ast.Nonlocal):
            declared_nonlocal.update(n.names)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            if any(_is_lock_expr(item.context_expr, lock_names) for item in n.items):
                regions.append((n.lineno, n.end_lineno or n.lineno))
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "acquire":
            uses_acquire |= _is_lock_expr(n.func.value, lock_names)
        elif isinstance(n, ast.Name):
            if isinstance(n.ctx, ast.Load):
                loads.append((n.id, n.lineno))
            else:
                local.add(n.id)
        elif isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load) and f.self_name:
            if isinstance(n.value, ast.Name) and n.value.id == f.self_name:
                loads.append((f"self.{n.attr}", n.lineno))
        elif isinstance(n, ast.ExceptHandler) and n.name:
            local.add(n.name)
        elif isinstance(n, (ast.Import, ast.ImportFrom)):
            local.update((a.asname or a.name).split(".")[0] for a in n.names)
    local -= declared_global | declared_nonlocal

    def module_state(name: str) -> bool:
        return name in mod.names and name not in local and name not in tls_names

    mutations: list[tuple[str, str, str, int, bool]] = []

    def add(scope: str, access: str, key: str, stmt: ast.stmt | ast.expr, aug: bool = False) -> None:
        value = getattr(stmt, "value", None)
        in_value = {k for k, _ in loads_in(value)} if value is not None else set()
        earlier = any(k == key and line < stmt.lineno for k, line in loads)
        mutations.append((scope, access, key, stmt.lineno, aug or earlier or key in in_value))

    def loads_in(expr: ast.expr | None):
        for sub in ast.walk(expr) if expr is not None else ():
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                yield sub.id, sub.lineno
            elif (
                isinstance(sub, ast.Attribute)
                and f.self_name
                and isinstance(sub.value, ast.Name)
                and sub.value.id == f.self_name
            ):
                yield f"self.{sub.attr}", sub.lineno

    def classify(target: ast.expr, stmt: ast.stmt, aug: bool) -> None:
        if isinstance(target, ast.Name):
            if target.id in declared_global:
                add("global", "rebind", target.id, stmt, aug)
            elif target.id in declared_nonlocal:
                add("closure", "rebind", target.id, stmt, aug)
        elif isinstance(target, ast.Attribute):
            attr = _self_attr(target, f.self_name)
            root = target
            while isinstance(root, (ast.Attribute, ast.Subscript)):
                root = root.value
            if attr is not None and f.node.name != "__init__":
                add("instance", "attr", f"self.{attr}", stmt, aug)
            elif isinstance(root, ast.Name) and module_state(root.id):
                add("global", "attr", root.id, stmt, aug)
        elif isinstance(target, ast.Subscript):
            attr = _self_attr(target, f.self_name)
            base = target.value
            if attr is not None and f.node.name != "__init__":
                add("instance", "item", f"self.{attr}", stmt, aug)
            elif isinstance(base, ast.Name) and module_state(base.id):
                add("global", "item", base.id, stmt, aug)

    for n in body:
        if isinstance(n, ast.Assign):
            for t in n.targets:
                for leaf in _flatten(t):
                    classify(leaf, n, False)
        elif isinstance(n, ast.AnnAssign) and n.value is not None:
            classify(n.target, n, False)
        elif isinstance(n, ast.AugAssign):
            classify(n.target, n, True)
        elif isinstance(n, ast.Delete):
            for t in n.targets:
                classify(t, n, False)
        elif isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr in MUTATORS:
            recv = n.func.value
            if isinstance(recv, ast.Name) and recv.id in mod.containers and module_state(recv.id):
                add("global", "call", recv.id, n)
            elif isinstance(recv, ast.Attribute) and f.node.name != "__init__":
                attr = _self_attr(recv, f.self_name)
                if attr is not None and isinstance(recv.value, ast.Name) and attr in class_containers.get(f.class_qualname or "", ()):
                    add("instance", "call", f"self.{attr}", n)
    return _Facts(regions, uses_acquire, mutations)


# --- Analysis -------------------------------------------------------------------------


def find_races(root: Path, repo: RepoScoutOutput) -> list[RaceIssue]:
    module_info = {m.path: m for m in repo.modules}
    funcs: dict[str, _Func] = {}
    classes: set[str] = set()
    calls_at: dict[tuple[str, int, int], ast.Call] = {}
    module_facts: dict[str, _ModuleFacts] = {}
    lock_names: set[str] = set()
    tls_names: set[str] = set()
    class_containers: dict[str, set[str]] = defaultdict(set)

    for path, info in module_info.items():
        try:
            tree = ast.parse((root / path).read_bytes(), filename=path)
        except (OSError, SyntaxError, ValueError):
            continue  # RepoScout already reported parse errors
        name, _ = module_name(path)
        collector = _Collector(name, path, info.is_test)
        collector.visit(tree)
        funcs.update(collector.funcs)
        classes |= collector.classes
        calls_at.update({(path, *pos): call for pos, call in collector.calls.items()})
        facts = module_facts[name] = _ModuleFacts()
        for stmt in tree.body:
            if isinstance(stmt, (ast.Assign, ast.AnnAssign)):
                targets = stmt.targets if isinstance(stmt, ast.Assign) else [stmt.target]
                for t in (leaf for target in targets for leaf in _flatten(target)):
                    if isinstance(t, ast.Name):
                        facts.names.add(t.id)
                        if _is_container(stmt.value):
                            facts.containers.add(t.id)
                        ctor = _call_name(stmt.value)
                        if ctor.rsplit(".", 1)[-1] in LOCK_CTORS:
                            lock_names.add(t.id)
                        elif ctor == "threading.local":
                            tls_names.add(t.id)
            elif isinstance(stmt, ast.AugAssign) and isinstance(stmt.target, ast.Name):
                facts.names.add(stmt.target.id)

    # `self.x = threading.Lock()` / `self.items = {}` anywhere in a class
    for f in funcs.values():
        if not f.self_name or not f.class_qualname:
            continue
        for n in _walk(f.node):
            if isinstance(n, ast.Assign) and len(n.targets) == 1:
                attr = _self_attr(n.targets[0], f.self_name) if isinstance(n.targets[0], ast.Attribute) else None
                if attr is None:
                    continue
                if _call_name(n.value).rsplit(".", 1)[-1] in LOCK_CTORS:
                    lock_names.add(f"self.{attr}")
                elif _is_container(n.value):
                    class_containers[f.class_qualname].add(attr)

    facts_of = {
        q: _function_facts(f, module_facts.get(f.module, _ModuleFacts()), class_containers, lock_names, tls_names)
        for q, f in funcs.items()
    }

    by_short: dict[str, list[str]] = defaultdict(list)
    for q in funcs:
        by_short[q.rsplit(".", 1)[-1]].append(q)

    def enclosing_class(qualname: str) -> str | None:
        parts = qualname.split(".")
        for i in range(len(parts), 0, -1):
            if ".".join(parts[:i]) in classes:
                return ".".join(parts[:i])
        return None

    def resolve_callable(expr: ast.expr, caller: str) -> str | None:
        if isinstance(expr, ast.Call) and _call_name(expr).rsplit(".", 1)[-1] == "partial" and expr.args:
            return resolve_callable(expr.args[0], caller)
        parts = caller.split(".")
        if isinstance(expr, ast.Name):
            for i in range(len(parts), 0, -1):
                candidate = f"{'.'.join(parts[:i])}.{expr.id}"
                if candidate in funcs:
                    return candidate
            matches = by_short.get(expr.id, [])
            return matches[0] if len(matches) == 1 else None
        if isinstance(expr, ast.Attribute):
            if isinstance(expr.value, ast.Name) and expr.value.id in ("self", "cls"):
                cls = enclosing_class(caller)
                if cls and f"{cls}.{expr.attr}" in funcs:
                    return f"{cls}.{expr.attr}"
            matches = [m for m in by_short.get(expr.attr, []) if funcs[m].class_qualname]
            return matches[0] if len(matches) == 1 else None
        return None

    def short(qualname: str) -> str:
        return qualname.removeprefix(funcs[qualname].module + ".") if qualname in funcs else qualname

    # 1. thread entry points
    entries: dict[str, str] = {}  # entry qualname -> how it was started
    for call in repo.calls:
        spec = SPAWNERS.get(call.target or "")
        if spec is None or call.resolution is not CallResolution.EXTERNAL:
            continue
        node = calls_at.get((call.location.file, call.location.line, call.location.column or 0))
        if node is None:
            continue
        keyword, index = spec
        arg = next((k.value for k in node.keywords if keyword and k.arg == keyword), None)
        if arg is None and len(node.args) > index:
            arg = node.args[index]
        entry = resolve_callable(arg, call.caller) if arg is not None else None
        if entry:
            spawner = (call.target or "").rsplit(".", 2)
            label = ".".join(spawner[-2:]) if len(spawner) > 1 and spawner[-1][:1].islower() else spawner[-1]
            entries.setdefault(entry, f"{short(call.caller)} -> {label}({short(entry)})")
    for s in repo.symbols:
        if s.kind is SymbolKind.CLASS and "threading.Thread" in s.bases and f"{s.qualname}.run" in funcs:
            entries.setdefault(f"{s.qualname}.run", f"{s.name} is a threading.Thread subclass (run)")

    # 2. everything reachable on a worker thread, not following calls made under a lock
    edges: dict[str, list[str]] = defaultdict(list)
    for call in repo.calls:
        if call.resolution is not CallResolution.INTERNAL or not call.target or call.caller not in funcs:
            continue
        target = call.target
        if target in classes and f"{target}.__init__" in funcs:
            target = f"{target}.__init__"
        if target in funcs and not _in_regions(call.location.line, facts_of[call.caller].lock_regions):
            edges[call.caller].append(target)

    parent: dict[str, str | None] = {e: None for e in entries}
    origin: dict[str, str] = {e: e for e in entries}
    queue = deque(entries)
    while queue:
        current = queue.popleft()
        for nxt in edges.get(current, ()):
            if nxt not in parent:
                parent[nxt] = current
                origin[nxt] = origin[current]
                queue.append(nxt)

    # 3. unprotected writes in that code
    issues: list[RaceIssue] = []
    for qualname in parent:
        f, facts = funcs[qualname], facts_of[qualname]
        if f.is_test or facts.uses_acquire:
            continue
        chain: list[str] = []
        cursor: str | None = qualname
        while cursor is not None:
            chain.append(cursor)
            cursor = parent[cursor]
        chain.reverse()
        seen: set[tuple[str, str]] = set()
        for scope, access, key, line, rmw in sorted(facts.mutations, key=lambda m: m[3]):
            if _in_regions(line, facts.lock_regions) or (scope, key) in seen:
                continue
            seen.add((scope, key))
            issues.append(RaceIssue(qualname, f.path, line, scope, access, key, rmw, entries[origin[qualname]], tuple(chain)))
    return issues


def describe(issue: RaceIssue) -> str:
    shown = [p.rsplit(".", 1)[-1] for p in issue.path]
    if len(shown) > MAX_PATH_SHOWN:
        shown = [*shown[:2], "...", *shown[-3:]]
    how = "read-modify-write" if issue.read_modify_write else "write"
    return (
        f"Runs on a worker thread: {issue.spawn}"
        + (f", then {' -> '.join(shown)}" if len(issue.path) > 1 else "")
        + f". `{issue.key}` is written at {issue.file}:{issue.line} ({how}) with no lock held, "
        "so concurrent threads can interleave and lose updates."
    )

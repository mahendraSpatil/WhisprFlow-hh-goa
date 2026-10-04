"""Static symbol index for a Python repo, built with ``ast``.

Pass 1 parses each file and records symbols, imports, name bindings and the
types it can infer cheaply (annotations, constructor calls, ``self.x = ...``).
Pass 2 resolves every call to the function it reaches:

- ``helper()``                   -> local/nested def, module def, or imported name
- ``mod.func()``                 -> via ``import mod`` / ``from pkg import mod``
- ``self.method()``              -> the enclosing class, then its bases
- ``self.repo.save()``           -> type of ``self.repo`` from ``__init__`` assignments
- ``conn.execute()``             -> type of ``conn`` from an annotation or ``conn = X(...)``
- ``super().save()``             -> the enclosing class's bases
- re-exports through ``__init__.py`` are followed

Library calls resolve to dotted external names (``sqlite3.Connection.execute``).
Anything dynamic stays unresolved rather than guessed.
"""

from __future__ import annotations

import ast
import builtins
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from app.models.agents import (
    CallResolution,
    CallSite,
    ImportRef,
    ModuleInfo,
    ParseError,
    SourceLocation,
    Symbol,
    SymbolKind,
)

BUILTIN_NAMES = frozenset(dir(builtins))
MAX_RESOLVE_DEPTH = 12
# Annotations that say nothing about which methods exist.
NON_TYPES = frozenset({"typing.Any", "typing_extensions.Any", "builtins.object"})
# Library factories whose return type matters downstream (e.g. spotting SQL execution).
KNOWN_FACTORY_RETURNS = {
    "sqlite3.connect": "sqlite3.Connection",
    "psycopg2.connect": "psycopg2.extensions.connection",
    "psycopg.connect": "psycopg.Connection",
    "pymysql.connect": "pymysql.connections.Connection",
    "threading.Lock": "threading.Lock",
    "threading.RLock": "threading.RLock",
}


@dataclass
class RepoIndex:
    modules: list[ModuleInfo] = field(default_factory=list)
    symbols: list[Symbol] = field(default_factory=list)
    calls: list[CallSite] = field(default_factory=list)
    parse_errors: list[ParseError] = field(default_factory=list)
    skipped_files: list[str] = field(default_factory=list)


def index_repository(root: Path, exclude_dirs: set[str], max_file_bytes: int) -> RepoIndex:
    index = RepoIndex()
    collected: list[_ModuleCollector] = []
    for path in iter_python_files(root, exclude_dirs):
        rel = path.relative_to(root).as_posix()
        if path.stat().st_size > max_file_bytes:
            index.skipped_files.append(rel)
            continue
        try:
            source = path.read_bytes()
            tree = ast.parse(source, filename=rel)
        except SyntaxError as exc:
            index.parse_errors.append(ParseError(path=rel, message=exc.msg, line=exc.lineno))
            continue
        except (UnicodeDecodeError, ValueError) as exc:
            index.parse_errors.append(ParseError(path=rel, message=str(exc)))
            continue
        module, is_package = module_name(rel)
        collector = _ModuleCollector(module, rel, is_package, loc=source.count(b"\n") + 1)
        collector.visit(tree)
        collected.append(collector)

    resolver = _Resolver(collected)
    for c in collected:
        index.modules.append(
            ModuleInfo(
                name=c.module,
                path=c.path,
                is_package=c.is_package,
                is_test=is_test_path(c.path),
                main_guard_line=c.main_guard_line,
                loc=c.loc,
                imports=c.imports,
                depends_on=resolver.internal_dependencies(c),
            )
        )
        index.symbols.extend(resolver.finalize_symbol(c, s) for s in c.symbols)
        index.calls.extend(resolver.resolve_call(call) for call in c.calls)
    return index


# --- Files and module names --------------------------------------------------


def iter_python_files(root: Path, exclude: set[str]) -> Iterator[Path]:
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in exclude and not d.endswith(".egg-info"))
        for filename in sorted(filenames):
            if filename.endswith(".py"):
                yield Path(dirpath, filename)


def module_name(rel: str) -> tuple[str, bool]:
    parts = rel.removesuffix(".py").split("/")
    if parts[0] == "src" and len(parts) > 1:
        parts = parts[1:]
    is_package = parts[-1] == "__init__"
    if is_package and len(parts) > 1:
        parts = parts[:-1]
    return ".".join(parts), is_package


def is_test_path(rel: str) -> bool:
    parts = rel.split("/")
    filename = parts[-1]
    return (
        any(p in ("test", "tests") for p in parts[:-1])
        or filename.startswith("test_")
        or filename.endswith("_test.py")
        or filename == "conftest.py"
    )


# --- AST helpers ------------------------------------------------------------


def _unparse(node: ast.AST, limit: int = 200) -> str:
    try:
        text = ast.unparse(node)
    except Exception:  # unparse can fail on unusual nodes; the index is best-effort
        text = type(node).__name__
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _dotted(node: ast.expr) -> str | None:
    """``a.b.c`` -> "a.b.c"; anything that is not a plain name chain -> None."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else None
    return None


def _annotation_type(node: ast.expr | None) -> str | None:
    """The single class an annotation names: ``X``, ``X | None``, ``Optional[X]``, ``"X"``."""
    if node is None:
        return None
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        try:
            return _annotation_type(ast.parse(node.value, mode="eval").body)
        except SyntaxError:
            return None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
        for side in (node.left, node.right):
            if not (isinstance(side, ast.Constant) and side.value is None):
                return _annotation_type(side)
        return None
    if isinstance(node, ast.Subscript) and _dotted(node.value) in ("Optional", "typing.Optional"):
        return _annotation_type(node.slice)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _dotted(node)
    return None


def _is_main_guard(test: ast.expr) -> bool:
    if not (isinstance(test, ast.Compare) and len(test.ops) == 1 and isinstance(test.ops[0], ast.Eq)):
        return False
    sides = [test.left, test.comparators[0]]
    has_name = any(isinstance(s, ast.Name) and s.id == "__name__" for s in sides)
    has_main = any(isinstance(s, ast.Constant) and s.value == "__main__" for s in sides)
    return has_name and has_main


_BRANCHES = (ast.If, ast.For, ast.AsyncFor, ast.While, ast.IfExp, ast.ExceptHandler, ast.Assert, ast.match_case)


def cyclomatic_complexity(func: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    """McCabe complexity of one function body; nested defs count toward their own symbol, not this one."""
    total = 1
    stack: list[ast.AST] = list(func.body)
    while stack:
        node = stack.pop()
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        if isinstance(node, _BRANCHES):
            total += 1
        elif isinstance(node, ast.comprehension):
            total += 1 + len(node.ifs)
        elif isinstance(node, ast.BoolOp):
            total += len(node.values) - 1
        stack.extend(ast.iter_child_nodes(node))
    return total


# --- Pass 1: collect ---------------------------------------------------------


@dataclass
class _Scope:
    """A function body, or the module's top level. Types are unresolved dotted names."""

    qualname: str
    self_name: str | None = None  # "self"/"cls" when inside a method (or a closure within one)
    self_class: str | None = None
    local_types: dict[str, str] = field(default_factory=dict)
    enclosing: list[str] = field(default_factory=list)  # this def and its parents, innermost first
    refs: set[str] = field(default_factory=set)  # dotted names used without being called
    locals: set[str] = field(default_factory=set)  # parameters and assigned names


@dataclass
class _ClassInfo:
    qualname: str
    module: str
    bases: list[str]
    attr_types: dict[str, str] = field(default_factory=dict)


@dataclass
class _RawCall:
    module: str
    scope: _Scope
    enclosing: list[str]  # qualnames of enclosing defs, innermost first
    form: str  # "name" | "attr" | "super" | "other"
    parts: list[str]
    text: str
    location: SourceLocation


class _ModuleCollector(ast.NodeVisitor):
    def __init__(self, module: str, path: str, is_package: bool, loc: int) -> None:
        self.module = module
        self.path = path
        self.is_package = is_package
        self.loc = loc
        self.symbols: list[Symbol] = []
        self.scopes_by_qualname: dict[str, _Scope] = {}
        self.imports: list[ImportRef] = []
        self.calls: list[_RawCall] = []
        self.classes: dict[str, _ClassInfo] = {}
        self.returns: dict[str, str] = {}  # function qualname -> return annotation
        self.bindings: dict[str, str] = {}  # top-level name -> dotted target
        self.main_guard_line: int | None = None
        self.module_scope = _Scope(qualname=module)
        self._defs: list[tuple[str, SymbolKind]] = []
        self._scopes: list[_Scope] = [self.module_scope]

    # -- helpers

    def _qualname(self, name: str | None = None) -> str:
        parts = [self.module, *(n for n, _ in self._defs)]
        if name:
            parts.append(name)
        return ".".join(p for p in parts if p)

    def _enclosing(self) -> list[str]:
        names = [n for n, _ in self._defs]
        return [".".join([self.module, *names[:i]]) for i in range(len(names), 0, -1)]

    def _loc(self, node: ast.stmt | ast.expr) -> SourceLocation:
        return SourceLocation(file=self.path, line=node.lineno, end_line=node.end_lineno, column=node.col_offset)

    @property
    def _scope(self) -> _Scope:
        return self._scopes[-1]

    def _in_class_body(self) -> _ClassInfo | None:
        if self._defs and self._defs[-1][1] == SymbolKind.CLASS:
            return self.classes.get(self._qualname())
        return None

    def _value_type(self, value: ast.expr) -> str | None:
        if isinstance(value, ast.Call):
            return _dotted(value.func)
        if isinstance(value, ast.Name):
            return self._scope.local_types.get(value.id) or self.module_scope.local_types.get(value.id)
        return None

    def _record_target(self, target: ast.expr, type_name: str | None) -> None:
        if not type_name:
            return
        scope = self._scope
        if isinstance(target, ast.Name):
            scope.local_types[target.id] = type_name
        elif (
            isinstance(target, ast.Attribute)
            and isinstance(target.value, ast.Name)
            and scope.self_name
            and target.value.id == scope.self_name
            and scope.self_class in self.classes
        ):
            self.classes[scope.self_class].attr_types[target.attr] = type_name

    # -- definitions

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        qualname = self._qualname(node.name)
        bases = [b for b in (_dotted(base) for base in node.bases) if b]
        self.symbols.append(self._symbol(node, SymbolKind.CLASS, bases=bases))
        self.classes[qualname] = _ClassInfo(qualname, self.module, bases)
        if not self._defs:
            self.bindings[node.name] = qualname
        self._defs.append((node.name, SymbolKind.CLASS))
        self.generic_visit(node)
        self._defs.pop()

    def visit_FunctionDef(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        owner = self._in_class_body()
        kind = SymbolKind.METHOD if owner else SymbolKind.FUNCTION
        qualname = self._qualname(node.name)
        self.symbols.append(self._symbol(node, kind).model_copy(update={"complexity": cyclomatic_complexity(node)}))
        if not self._defs:
            self.bindings[node.name] = qualname
        if returns := _annotation_type(node.returns):
            self.returns[qualname] = returns

        # Decorators run in the enclosing scope, when the def statement executes.
        for decorator in node.decorator_list:
            self.visit(decorator)
        params = [*node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs]
        decorators = {_dotted(d) for d in node.decorator_list}
        if owner and params and "staticmethod" not in decorators:
            scope = _Scope(qualname, self_name=params[0].arg, self_class=owner.qualname)
            params = params[1:]
        else:  # plain function, or a closure that still sees the enclosing function's names
            parent = self._scope
            scope = _Scope(qualname, self_name=parent.self_name, self_class=parent.self_class)
            if parent is not self.module_scope:
                scope.local_types.update(parent.local_types)
        for param in params:
            if type_name := _annotation_type(param.annotation):
                scope.local_types[param.arg] = type_name
        scope.enclosing = [qualname, *self._enclosing()]
        scope.locals.update(a.arg for a in [*params, node.args.vararg, node.args.kwarg] if a)
        if scope.self_name:
            scope.locals.add(scope.self_name)
        self.scopes_by_qualname[qualname] = scope

        self._defs.append((node.name, kind))
        self._scopes.append(scope)
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        for stmt in node.body:
            self.visit(stmt)
        self._scopes.pop()
        self._defs.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def _symbol(
        self, node: ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef, kind: SymbolKind, bases: list[str] | None = None
    ) -> Symbol:
        return Symbol(
            qualname=self._qualname(node.name),
            name=node.name,
            kind=kind,
            module=self.module,
            location=self._loc(node),
            is_async=isinstance(node, ast.AsyncFunctionDef),
            decorators=[_unparse(d) for d in node.decorator_list],
            parent=self._qualname() if self._defs else None,
            bases=bases or [],
        )

    # -- imports

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self.imports.append(ImportRef(module=alias.name, line=node.lineno))
            if alias.asname:
                self.bindings[alias.asname] = alias.name
            else:
                top = alias.name.split(".")[0]
                self.bindings[top] = top

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        module = node.module or ""
        if node.level:
            package = self.module.split(".") if self.is_package else self.module.split(".")[:-1]
            base = package[: max(len(package) - (node.level - 1), 0)]
            module = ".".join([*base, module] if module else base)
        self.imports.append(
            ImportRef(module=module, names=[a.name for a in node.names], level=node.level, line=node.lineno)
        )
        for alias in node.names:
            if alias.name != "*":
                self.bindings[alias.asname or alias.name] = f"{module}.{alias.name}" if module else alias.name

    # -- assignments that tell us a type

    def visit_Assign(self, node: ast.Assign) -> None:
        if len(node.targets) == 1:
            self._record_target(node.targets[0], self._value_type(node.value))
        self.generic_visit(node)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        type_name = _annotation_type(node.annotation)
        owner = self._in_class_body()
        if owner and isinstance(node.target, ast.Name):
            if type_name:
                owner.attr_types[node.target.id] = type_name  # class-level / dataclass field
        else:
            self._record_target(node.target, type_name)
        self.generic_visit(node)

    def visit_With(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None:
                self._record_target(item.optional_vars, self._value_type(item.context_expr))
        self.generic_visit(node)

    visit_AsyncWith = visit_With

    # -- calls and the main guard

    def visit_Call(self, node: ast.Call) -> None:
        func = node.func
        form, parts = "other", []
        if isinstance(func, ast.Name):
            form, parts = "name", [func.id]
        elif isinstance(func, ast.Attribute):
            if dotted := _dotted(func):
                form, parts = "attr", dotted.split(".")
            elif (
                isinstance(func.value, ast.Call)
                and isinstance(func.value.func, ast.Name)
                and func.value.func.id == "super"
            ):
                form, parts = "super", [func.attr]
        self.calls.append(
            _RawCall(
                module=self.module,
                scope=self._scope,
                enclosing=self._enclosing(),
                form=form,
                parts=parts,
                text=_unparse(func),
                location=self._loc(node),
            )
        )
        # The callee itself is a call, not a reference; only descend into what it is built from.
        if form in ("name", "attr"):
            for child in [*node.args, *node.keywords]:
                self.visit(child)
        else:
            self.generic_visit(node)

    # -- references (names used without being called: callbacks, annotations, base classes)

    def visit_Name(self, node: ast.Name) -> None:
        if isinstance(node.ctx, ast.Load):
            self._scope.refs.add(node.id)
        else:
            self._scope.locals.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.ctx, ast.Load) and (dotted := _dotted(node)):
            self._scope.refs.add(dotted)
        else:
            self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        if not self._defs and self.main_guard_line is None and _is_main_guard(node.test):
            self.main_guard_line = node.lineno
        self.generic_visit(node)


# --- Pass 2: resolve ---------------------------------------------------------


class _Resolver:
    def __init__(self, collected: list[_ModuleCollector]) -> None:
        self.modules = {c.module: c for c in collected}
        self.symbols: dict[str, Symbol] = {s.qualname: s for c in collected for s in c.symbols}
        self.classes: dict[str, _ClassInfo] = {q: info for c in collected for q, info in c.classes.items()}
        self.returns: dict[str, tuple[str, str]] = {
            q: (c.module, r) for c in collected for q, r in c.returns.items()
        }
        self._bases_cache: dict[str, list[str]] = {}

    # -- names

    def _internal_prefix(self, dotted: str) -> str | None:
        parts = dotted.split(".")
        for i in range(len(parts), 0, -1):
            candidate = ".".join(parts[:i])
            if candidate in self.modules:
                return candidate
        return None

    def canonical(self, dotted: str, depth: int = 0) -> str:
        """Follow re-exports: "pkg.Name" -> "pkg.mod.Name" when pkg/__init__.py imports Name from mod."""
        if depth > MAX_RESOLVE_DEPTH or dotted in self.symbols or dotted in self.modules:
            return dotted
        prefix = self._internal_prefix(dotted)
        if not prefix:
            return dotted
        head, _, tail = dotted[len(prefix) + 1 :].partition(".")
        target = self.modules[prefix].bindings.get(head)
        if not target:
            return dotted
        return self.canonical(f"{target}.{tail}" if tail else target, depth + 1)

    def resolve_ref(self, module: str, dotted: str) -> str | None:
        """What a dotted name written in ``module`` refers to, as a canonical dotted name."""
        head, _, tail = dotted.partition(".")
        base = self.modules[module].bindings.get(head)
        if base is None:
            return f"builtins.{dotted}" if head in BUILTIN_NAMES else None
        return self.canonical(f"{base}.{tail}" if tail else base)

    def is_internal(self, dotted: str) -> bool:
        return dotted in self.symbols or self._internal_prefix(dotted) is not None

    # -- types

    def resolve_type(self, module: str, type_name: str, depth: int = 0) -> str | None:
        """A class qualname (internal) or dotted class name (external) for a type expression."""
        if depth > MAX_RESOLVE_DEPTH:
            return None
        ref = self.resolve_ref(module, type_name)
        if ref is None or ref in NON_TYPES:
            return None
        if ref in KNOWN_FACTORY_RETURNS:
            return KNOWN_FACTORY_RETURNS[ref]
        if ref in self.classes:
            return ref
        if ref in self.returns:  # a factory function: use its return annotation
            ret_module, ret_type = self.returns[ref]
            return self.resolve_type(ret_module, ret_type, depth + 1)
        if not self.is_internal(ref) and ref.rsplit(".", 1)[-1][:1].isupper():
            return ref  # external class, by naming convention
        return None

    def class_bases(self, cls: str) -> list[str]:
        if cls not in self._bases_cache:
            info = self.classes[cls]
            resolved = (self.resolve_ref(info.module, b) or b for b in info.bases)
            self._bases_cache[cls] = [b for b in resolved if b != "builtins.object"]
        return self._bases_cache[cls]

    def find_member(self, cls: str, name: str, seen: set[str] | None = None) -> tuple[str, bool] | None:
        """(target, is_internal) for ``cls.name``, searching bases depth-first."""
        if cls not in self.classes:
            return (f"{cls}.{name}", False) if not self.is_internal(cls) else None
        seen = seen or set()
        if cls in seen:
            return None
        seen.add(cls)
        if f"{cls}.{name}" in self.symbols:
            return f"{cls}.{name}", True
        for base in self.class_bases(cls):
            if found := self.find_member(base, name, seen):
                return found
        return None

    def attribute_type(self, cls: str, attr: str, seen: set[str] | None = None) -> str | None:
        if cls not in self.classes:
            return None
        seen = seen or set()
        if cls in seen:
            return None
        seen.add(cls)
        info = self.classes[cls]
        if attr in info.attr_types:
            return self.resolve_type(info.module, info.attr_types[attr])
        for base in self.class_bases(cls):
            if found := self.attribute_type(base, attr, seen):
                return found
        return None

    def _variable_type(self, module: str, scope: _Scope, name: str) -> str | None:
        if scope.self_name and name == scope.self_name and scope.self_class:
            return scope.self_class
        module_scope = self.modules[module].module_scope
        type_name = scope.local_types.get(name) or module_scope.local_types.get(name)
        return self.resolve_type(module, type_name) if type_name else None

    def _member_of_variable(self, module: str, scope: _Scope, parts: list[str]) -> tuple[str | None, CallResolution] | None:
        """Resolve ``var.attr...member`` through the variable's type; None when the variable is untyped."""
        base, *attrs = parts
        owner = self._variable_type(module, scope, base)
        if owner is None or not attrs:
            return None
        for attr in attrs[:-1]:
            owner = self.attribute_type(owner, attr)
            if owner is None:
                return None, CallResolution.UNRESOLVED
        return self._member(owner, attrs[-1])

    # -- calls

    def resolve_call(self, call: _RawCall) -> CallSite:
        target, resolution = self._resolve(call)
        return CallSite(
            caller=call.scope.qualname,
            callee=call.text,
            target=target,
            resolution=resolution,
            location=call.location,
        )

    def _classify(self, ref: str | None) -> tuple[str | None, CallResolution]:
        if ref is None:
            return None, CallResolution.UNRESOLVED
        if ref in self.symbols:
            return ref, CallResolution.INTERNAL
        if self.is_internal(ref):
            return None, CallResolution.UNRESOLVED  # names something in this repo we did not index
        return ref, CallResolution.EXTERNAL

    def _resolve(self, call: _RawCall) -> tuple[str | None, CallResolution]:
        if call.form == "name":
            name = call.parts[0]
            for scope in call.enclosing:  # nested defs shadow module-level names
                candidate = f"{scope}.{name}"
                if candidate in self.symbols and self.symbols[candidate].kind != SymbolKind.METHOD:
                    return candidate, CallResolution.INTERNAL
            return self._classify(self.resolve_ref(call.module, name))

        if call.form == "attr":
            via_type = self._member_of_variable(call.module, call.scope, call.parts)
            if via_type is not None:
                return via_type
            return self._classify(self.resolve_ref(call.module, ".".join(call.parts)))

        if call.form == "super" and call.scope.self_class in self.classes:
            for base in self.class_bases(call.scope.self_class):
                if found := self._member(base, call.parts[0]):
                    if found[0] is not None:
                        return found
        return None, CallResolution.UNRESOLVED

    def _member(self, owner: str, name: str) -> tuple[str | None, CallResolution]:
        found = self.find_member(owner, name)
        if found is None:
            return None, CallResolution.UNRESOLVED
        target, internal = found
        return target, CallResolution.INTERNAL if internal else CallResolution.EXTERNAL

    # -- module-level results

    def internal_dependencies(self, c: _ModuleCollector) -> list[str]:
        deps: set[str] = set()
        for imp in c.imports:
            targets = [t for t in (f"{imp.module}.{n}" for n in imp.names) if t in self.modules]
            if not targets and (prefix := self._internal_prefix(imp.module)):
                targets = [prefix]
            deps.update(t for t in targets if t != c.module)
        return sorted(deps)

    def finalize_symbol(self, c: _ModuleCollector, symbol: Symbol) -> Symbol:
        if symbol.kind == SymbolKind.CLASS:
            return symbol.model_copy(update={"bases": self.class_bases(symbol.qualname)})
        scope = c.scopes_by_qualname.get(symbol.qualname)
        if scope is None:
            return symbol
        return symbol.model_copy(update={"references": self.resolve_references(c.module, scope)})

    def resolve_references(self, module: str, scope: _Scope) -> list[str]:
        """Internal symbols and library names this function uses without calling them here."""
        found: set[str] = set()
        for dotted in scope.refs:
            parts = dotted.split(".")
            if len(parts) > 1 and (via_type := self._member_of_variable(module, scope, parts)):
                if via_type[0]:
                    found.add(via_type[0])  # e.g. a bound method passed as a callback: api.create_order
                continue
            if parts[0] in scope.locals or parts[0] in scope.local_types:
                continue  # a parameter or local variable, not a reference to something defined elsewhere
            nested = next(
                (f"{s}.{dotted}" for s in scope.enclosing if f"{s}.{dotted}" in self.symbols), None
            )
            target = nested or self.resolve_ref(module, dotted)
            if target and not target.startswith("builtins.") and (target in self.symbols or not self.is_internal(target)):
                found.add(target)
        return sorted(found)

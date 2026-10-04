from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.agents.indexer import index_repository
from app.models.agents import CallResolution, SymbolKind

FILES = {
    "shop/__init__.py": "from shop.models import Item\n",
    "shop/models.py": """
        import sqlite3

        class Base:
            def save(self):
                return self.validate()

            def validate(self):
                return True

        class Item(Base):
            label: str

            def save(self):
                return super().save()

        def open_db(path: str) -> sqlite3.Connection:
            return sqlite3.connect(path)
    """,
    "shop/repo.py": """
        from typing import Any, Optional
        import shop.models as m
        from . import models
        from shop import Item

        class Repo:
            def __init__(self, conn: "sqlite3.Connection", fallback: Optional[models.Base] = None):
                self.conn = conn
                self.fallback = fallback
                self.db = models.open_db(":memory:")

            def add(self, item: Item, extra: Any):
                item.save()
                extra.anything()
                self.fallback.validate()
                self.db.execute("INSERT ...")
                return self._helper()

            def _helper(self):
                def inner():
                    return self.conn.commit()
                return inner()

        def build():
            repo = Repo(m.open_db("x"))
            with models.open_db("y") as conn:
                conn.execute("SELECT 1")
            repo.add(Item(), None)
            return len([])
    """,
}


@pytest.fixture
def index(tmp_path: Path):
    for rel, content in FILES.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    return index_repository(tmp_path, set(), 1_000_000)


def calls_from(index, caller: str) -> dict[str, tuple[str | None, CallResolution]]:
    return {c.callee: (c.target, c.resolution) for c in index.calls if c.caller == caller}


def test_symbols_have_kinds_lines_and_resolved_bases(index):
    symbols = {s.qualname: s for s in index.symbols}
    assert symbols["shop.models.Item"].kind is SymbolKind.CLASS
    assert symbols["shop.models.Item"].bases == ["shop.models.Base"]
    assert symbols["shop.models.Item.save"].kind is SymbolKind.METHOD
    assert symbols["shop.models.Item.save"].location.line == 14
    assert symbols["shop.repo.Repo._helper.inner"].kind is SymbolKind.FUNCTION
    assert symbols["shop.repo.Repo._helper.inner"].parent == "shop.repo.Repo._helper"


def test_internal_dependencies_follow_relative_and_aliased_imports(index):
    deps = {m.name: m.depends_on for m in index.modules}
    assert deps["shop.repo"] == ["shop", "shop.models"]
    assert deps["shop"] == ["shop.models"]


def test_method_calls_resolve_through_types_and_inheritance(index):
    add = calls_from(index, "shop.repo.Repo.add")
    # parameter annotation, re-exported through shop/__init__.py
    assert add["item.save"] == ("shop.models.Item.save", CallResolution.INTERNAL)
    # Optional[...] annotation, method found on the class itself
    assert add["self.fallback.validate"] == ("shop.models.Base.validate", CallResolution.INTERNAL)
    # attribute typed by a factory's return annotation
    assert add["self.db.execute"] == ("sqlite3.Connection.execute", CallResolution.EXTERNAL)
    assert add["self._helper"] == ("shop.repo.Repo._helper", CallResolution.INTERNAL)
    # Any tells us nothing
    assert add["extra.anything"] == (None, CallResolution.UNRESOLVED)


def test_inherited_and_super_calls(index):
    assert calls_from(index, "shop.models.Base.save")["self.validate"] == (
        "shop.models.Base.validate",
        CallResolution.INTERNAL,
    )
    assert calls_from(index, "shop.models.Item.save")["super().save"] == (
        "shop.models.Base.save",
        CallResolution.INTERNAL,
    )


def test_closures_see_the_enclosing_methods_self(index):
    helper = calls_from(index, "shop.repo.Repo._helper")
    assert helper["inner"] == ("shop.repo.Repo._helper.inner", CallResolution.INTERNAL)
    inner = calls_from(index, "shop.repo.Repo._helper.inner")
    # string annotation "sqlite3.Connection" without an import of sqlite3 in this module stays unresolved
    assert inner["self.conn.commit"] == (None, CallResolution.UNRESOLVED)


def test_module_aliases_locals_and_with_targets(index):
    build = calls_from(index, "shop.repo.build")
    assert build["Repo"] == ("shop.repo.Repo", CallResolution.INTERNAL)
    assert build["m.open_db"] == ("shop.models.open_db", CallResolution.INTERNAL)
    assert build["models.open_db"] == ("shop.models.open_db", CallResolution.INTERNAL)
    assert build["conn.execute"] == ("sqlite3.Connection.execute", CallResolution.EXTERNAL)
    assert build["repo.add"] == ("shop.repo.Repo.add", CallResolution.INTERNAL)
    assert build["Item"] == ("shop.models.Item", CallResolution.INTERNAL)
    assert build["len"] == ("builtins.len", CallResolution.EXTERNAL)


def test_parse_errors_are_reported_not_fatal(tmp_path):
    (tmp_path / "ok.py").write_text("def f():\n    return 1\n")
    (tmp_path / "bad.py").write_text("def broken(:\n")
    index = index_repository(tmp_path, set(), 1_000_000)
    assert [m.name for m in index.modules] == ["ok"]
    assert [(e.path, e.line) for e in index.parse_errors] == [("bad.py", 1)]

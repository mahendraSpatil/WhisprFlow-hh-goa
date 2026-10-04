from __future__ import annotations

import textwrap
from pathlib import Path

from app.agents.indexer import index_repository
from app.agents.system_analyst import analyze
from app.models.agents import (
    ArchitectureStyle,
    Layer,
    RepoScoutOutput,
    SourceKind,
    SystemAnalystInput,
)


def analyze_files(root: Path, files: dict[str, str]):
    for rel, content in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(content), encoding="utf-8")
    index = index_repository(root, set(), 1_000_000)
    repo = RepoScoutOutput(
        root=root, source_kind=SourceKind.LOCAL, modules=index.modules, symbols=index.symbols, calls=index.calls
    )
    return analyze(SystemAnalystInput(root=root, repo=repo))


FLASK_APP = {
    "requirements.txt": "Flask==3.0.0  # web\nrequests>=2.31\n-r dev.txt\n",
    "app/__init__.py": "",
    "app/routes.py": """
        from flask import Flask
        from app.logic import checkout
        app = Flask(__name__)

        @app.route("/checkout", methods=["POST"])
        def post_checkout():
            return checkout()
    """,
    "app/logic.py": """
        import threading
        import requests
        from app.storage import save
        _lock = threading.Lock()

        def checkout():
            requests.post("https://payments.example")
            return save()
    """,
    "app/storage.py": """
        import sqlite3
        from app.helpers import now

        def save():
            conn = sqlite3.connect("x.db")
            conn.execute("INSERT INTO orders VALUES (?)", (now(),))
    """,
    "app/helpers.py": "def now():\n    return 0\n",
    "app/extras.py": "from app.helpers import now\n",
    "manage.py": """
        from app.routes import app
        if __name__ == "__main__":
            app.run()
    """,
    "tests/test_logic.py": "import pytest\nfrom app.logic import checkout\n",
}


def test_stack_from_requirements_and_imports(tmp_path):
    out = analyze_files(tmp_path, FLASK_APP)
    stack = {c.key: c for c in out.stack}

    assert stack["flask"].declared and stack["flask"].used_in_app
    assert stack["flask"].evidence[0] == "requirements.txt: Flask==3.0.0"
    assert stack["requests"].category == "http_client" and stack["requests"].used_in_app
    assert stack["sqlite3"].category == "database" and not stack["sqlite3"].declared
    assert stack["threading"].category == "concurrency"
    assert stack["pytest"].used_in_tests and not stack["pytest"].used_in_app
    assert out.dependencies == ["flask", "requests"]


def test_layers(tmp_path):
    out = analyze_files(tmp_path, FLASK_APP)
    layers = {l.module: l.layer for l in out.layers}

    assert layers == {
        "app": Layer.UTIL,
        "app.routes": Layer.API,
        "app.logic": Layer.SERVICE,
        "app.storage": Layer.DATA,
        "app.helpers": Layer.UTIL,
        "app.extras": Layer.UTIL,
        "manage": Layer.ENTRY,
        "tests.test_logic": Layer.TEST,
    }
    storage = next(l for l in out.layers if l.module == "app.storage")
    assert "1 database calls" in storage.evidence
    assert not any(d.upward for d in out.layer_dependencies)


def test_upward_dependency_is_flagged(tmp_path):
    files = dict(FLASK_APP)
    files["app/storage.py"] = (
        textwrap.dedent(files["app/storage.py"]) + "from app.routes import app as _app  # data reaching into api\n"
    )
    out = analyze_files(tmp_path, files)
    upward = [(d.source, d.target) for d in out.layer_dependencies if d.upward]
    assert upward == [(Layer.DATA, Layer.API)]
    assert "Upward dependencies: data -> api." in out.summary


def test_architecture_and_summary(tmp_path):
    out = analyze_files(tmp_path, FLASK_APP)
    assert out.architecture is ArchitectureStyle.WEB_SERVICE
    assert out.summary == (
        "Web service on Flask with SQLite storage. "
        "7 application modules layered entry -> api -> service -> data (+3 util). "
        "Concurrency: threading. Outbound HTTP: Requests. Tests: pytest, 1 test modules."
    )


def test_library_without_tests(tmp_path):
    out = analyze_files(tmp_path, {"lib/core.py": "def add(a, b):\n    return a + b\n"})
    assert out.architecture is ArchitectureStyle.LIBRARY
    assert out.test_runner is None
    assert out.summary.endswith("No tests found.")

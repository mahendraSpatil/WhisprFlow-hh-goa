from __future__ import annotations

from pathlib import Path

import pytest

SAMPLE_FILES = {
    "pyproject.toml": '[project]\nname = "sample"\nrequires-python = ">=3.11"\ndependencies = ["fastapi>=0.100"]\n',
    "sample/__init__.py": "",
    "sample/api.py": (
        "from fastapi import FastAPI\n"
        "from .service import total\n\n"
        "app = FastAPI()\n\n"
        "@app.get('/total')\n"
        "def get_total(n: int) -> int:\n"
        "    return total(n)\n"
    ),
    "sample/service.py": (
        "def total(n):\n"
        "    return sum(_items(n))\n\n"
        "def _items(n):\n"
        "    return range(n)\n\n"
        "if __name__ == '__main__':\n"
        "    print(total(3))\n"
    ),
    "sample/broken.py": "def oops(:\n",
    "tests/test_service.py": "import pytest\nfrom sample.service import total\n\ndef test_total():\n    assert total(3) == 3\n",
}


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    root = tmp_path / "sample_repo"
    for rel, content in SAMPLE_FILES.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root


@pytest.fixture(autouse=True)
def no_llm_calls(monkeypatch):
    """Tests must never send code to the Anthropic API, whatever credentials the machine has."""
    monkeypatch.setenv("CODELOOP_LLM", "off")


@pytest.fixture(autouse=True)
def isolated_memory(monkeypatch, tmp_path_factory):
    """Every test gets its own incident memory, so none reads or writes the real one in the user's home."""
    monkeypatch.setenv("CODELOOP_MEMORY_DB", str(tmp_path_factory.mktemp("memory") / "memory.db"))
    monkeypatch.delenv("CODELOOP_MEMORY", raising=False)


@pytest.fixture(autouse=True)
def sandbox_python(monkeypatch):
    """Run sandboxed tests with this interpreter, which has pytest; building a virtualenv per test would take minutes."""
    import sys

    monkeypatch.setenv("CODELOOP_SANDBOX_PYTHON", sys.executable)


@pytest.fixture(autouse=True)
def no_real_github(monkeypatch):
    """No test may use a real GitHub token or reach api.github.com; tests that need GitHub point at a local fake."""
    for name in ("GITHUB_TOKEN", "GITHUB_API_URL", "CODELOOP_GITHUB_REPO"):
        monkeypatch.delenv(name, raising=False)
    from app import github_pr

    monkeypatch.setattr(github_pr, "SECONDS_BETWEEN_REQUESTS", 0)  # the polite pauses are for the real GitHub
    monkeypatch.setattr(github_pr, "SECONDS_BETWEEN_WRITES", 0)

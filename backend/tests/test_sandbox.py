from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from helpers import Log, write_files

from app.models.agents import CaseOutcome
from app.sandbox import Sandbox, clean_environment, ensure_environment, fresh_copy, run_suite
from app.models.agents import Isolation

SANDBOX = Sandbox(Path(sys.executable), Isolation.SUBPROCESS)


def run(tmp_path, files, timeout_s=60):
    repo = tmp_path / "repo"
    write_files(repo, files)
    return run_suite(SANDBOX, repo, ["tests"], Log(), timeout_s=timeout_s)


def outcomes(out):
    return {c.node_id.split("::")[1]: c.outcome for c in out.tests}


def test_results_totals_and_tracebacks(tmp_path):
    out = run(tmp_path, {
        "shop/__init__.py": "",
        "shop/calc.py": "def ratio(n):\n    return 100 // n\n",
        "tests/test_calc.py": (
            "import pytest\nfrom shop import calc\n\n"
            "def test_ok():\n    assert calc.ratio(10) == 10\n\n"
            "def test_boom():\n    calc.ratio(0)\n\n"
            "def test_assertion():\n    assert calc.ratio(5) == 21\n\n"
            "@pytest.mark.skip(reason='later')\ndef test_skipped():\n    pass\n"
        ),
    })
    assert outcomes(out) == {
        "test_ok": CaseOutcome.PASSED, "test_boom": CaseOutcome.FAILED,
        "test_assertion": CaseOutcome.FAILED, "test_skipped": CaseOutcome.SKIPPED,
    }
    assert (out.totals.passed, out.totals.failed, out.totals.errors, out.totals.skipped) == (1, 2, 0, 1)

    boom = next(c for c in out.tests if c.node_id.endswith("test_boom"))
    assert boom.exception.type == "ZeroDivisionError"
    # innermost frame last: the application code, as qualified function names the graph understands
    assert [(f.file, f.line, f.function) for f in boom.exception.frames] == [
        ("tests/test_calc.py", 8, "test_calc.test_boom"),
        ("shop/calc.py", 2, "shop.calc.ratio"),
    ]
    assert out.isolation is Isolation.SUBPROCESS and out.duration_s > 0


def test_a_collection_error_is_reported_not_swallowed(tmp_path):
    out = run(tmp_path, {"tests/test_broken.py": "def test_x(:\n    pass\n", "tests/test_fine.py": "def test_y():\n    pass\n"})
    assert out.totals.passed == 1 and out.totals.errors == 1
    broken = next(c for c in out.tests if c.outcome is CaseOutcome.ERROR)
    assert "test_broken.py" in broken.node_id and "SyntaxError" in broken.message


def test_a_suite_with_no_tests_is_empty_not_an_error(tmp_path):
    out = run(tmp_path, {"tests/helpers.py": "X = 1\n"})
    assert out.tests == [] and out.totals.passed == 0


def test_a_hung_suite_is_killed_and_keeps_the_results_that_arrived(tmp_path):
    out = run(
        tmp_path,
        {"tests/test_slow.py": "import time\n\ndef test_fast():\n    pass\n\ndef test_hangs():\n    time.sleep(120)\n"},
        timeout_s=4,
    )
    by_name = {c.node_id: c for c in out.tests}
    assert by_name["tests/test_slow.py::test_fast"].outcome is CaseOutcome.PASSED
    assert by_name["<timeout>"].outcome is CaseOutcome.ERROR and "timed out after 4s" in by_name["<timeout>"].message
    assert out.duration_s < 30  # the whole process tree went down, not just the first process


def test_network_and_writes_outside_the_sandbox_are_blocked(tmp_path, monkeypatch):
    outside = Path.home() / "codeloop_sandbox_must_not_write_here.txt"
    outside.unlink(missing_ok=True)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-should-not-leak")
    monkeypatch.setenv("MY_SERVICE_TOKEN", "should-not-leak")
    monkeypatch.setenv("HARMLESS_SETTING", "visible")
    out = run(tmp_path, {
        "tests/test_guard.py": (
            "import os, socket, pytest\n\n"
            "def test_connect_is_blocked():\n"
            "    with pytest.raises(Exception, match='blocked'):\n        socket.create_connection(('93.184.216.34', 80), timeout=2)\n\n"
            "def test_dns_is_blocked():\n"
            "    with pytest.raises(Exception, match='blocked'):\n        socket.getaddrinfo('example.com', 80)\n\n"
            "def test_loopback_is_allowed():\n"
            "    s = socket.socket(); s.bind(('127.0.0.1', 0)); s.close()\n\n"
            f"def test_write_outside_is_blocked():\n    with pytest.raises(Exception, match='blocked'):\n        open({str(outside)!r}, 'w')\n\n"
            "def test_write_inside_the_copy_is_allowed():\n    open('scratch.txt', 'w').write('x')\n\n"
            "def test_credentials_are_not_inherited():\n"
            "    assert 'ANTHROPIC_API_KEY' not in os.environ and 'MY_SERVICE_TOKEN' not in os.environ\n"
            "    assert os.environ['HARMLESS_SETTING'] == 'visible'\n"
            "    assert os.environ['HTTP_PROXY'].endswith(':9')\n"
        ),
    })
    assert out.totals.failed == 0 and out.totals.errors == 0 and out.totals.passed == 6, [
        (c.node_id, c.message) for c in out.tests if c.outcome is not CaseOutcome.PASSED
    ]
    assert not outside.exists()


def test_the_environment_is_stripped_of_credentials(monkeypatch):
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    monkeypatch.setenv("DB_PASSWORD", "x")
    monkeypatch.setenv("CODELOOP_LLM", "on")
    monkeypatch.setenv("PLAIN", "keep")
    env = clean_environment()
    assert "PLAIN" in env and "PATH" in env or os.name == "nt"
    assert not {"GITHUB_TOKEN", "DB_PASSWORD", "CODELOOP_LLM"} & set(env)


def test_a_fresh_copy_is_private_and_clean(tmp_path):
    src = tmp_path / "src"
    write_files(src, {"a.py": "x = 1\n", ".git/config": "[core]\n", "__pycache__/a.pyc": "junk", "node_modules/x.js": "x"})
    copy = fresh_copy(src, tmp_path / "work" / "repo")
    (copy / "a.py").write_text("x = 2\n")
    assert (src / "a.py").read_text() == "x = 1\n"
    assert sorted(p.name for p in copy.iterdir()) == ["a.py"]
    (copy / "stale.py").write_text("")
    assert not (fresh_copy(src, copy) / "stale.py").exists()  # replaced, not merged


def test_the_interpreter_override_skips_building_a_virtualenv(tmp_path):
    sandbox = ensure_environment(tmp_path, ["pytest"], Log())
    assert sandbox.python == Path(sys.executable) and not (tmp_path / "venv").exists()


def test_an_invalid_interpreter_override_rejected(tmp_path, monkeypatch):
    monkeypatch.setenv("CODELOOP_SANDBOX_PYTHON", str(tmp_path / "no-python-here"))
    with pytest.raises(RuntimeError, match="CODELOOP_SANDBOX_PYTHON"):
        ensure_environment(tmp_path, ["pytest"], Log())


@pytest.mark.skipif(os.environ.get("CODELOOP_SLOW_TESTS") != "1", reason="creates a real virtualenv and needs network (CODELOOP_SLOW_TESTS=1)")
def test_a_real_fresh_virtualenv_runs_the_tests(tmp_path, monkeypatch):
    monkeypatch.delenv("CODELOOP_SANDBOX_PYTHON")
    log = Log()
    sandbox = ensure_environment(tmp_path, [], log)
    assert sandbox.python.exists() and sandbox.python != Path(sys.executable)
    assert ensure_environment(tmp_path, [], log).python == sandbox.python  # reused, not rebuilt
    repo = tmp_path / "repo"
    write_files(repo, {"tests/test_x.py": "def test_x():\n    assert True\n"})
    assert run_suite(sandbox, repo, ["tests"], log).totals.passed == 1

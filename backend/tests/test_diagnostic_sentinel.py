from __future__ import annotations

import pytest
from helpers import build, write_files

from app.agents.bandit_scan import BanditError
from app.agents.diagnostic_sentinel import DiagnosticSentinel, diagnose
from app.agents.race_check import find_races
from app.models.agents import (
    CaseOutcome,
    CaseResult,
    DiagnosticSentinelInput,
    ExceptionInfo,
    FindingCategory,
    FindingSource,
    Isolation,
    SandboxRunnerOutput,
    Severity,
    SourceStatus,
    StackFrame,
    SyntheticProbe,
)

# --- Race condition checker -----------------------------------------------------------


def races(tmp_path, files):
    repo, _, _ = build(tmp_path, files)
    return {(i.function.rsplit(".", 1)[-1], i.key): i for i in find_races(tmp_path, repo)}


def test_threaded_global_counter_and_container(tmp_path):
    found = races(tmp_path, {
        "app.py": """
            import threading
            COUNTER = 0
            RESULTS = []

            def bump():
                global COUNTER
                COUNTER += 1
                RESULTS.append(COUNTER)

            def main():
                threads = [threading.Thread(target=bump) for _ in range(4)]
                for t in threads:
                    t.start()
        """
    })
    counter, results = found[("bump", "COUNTER")], found[("bump", "RESULTS")]
    assert (counter.rule_id, counter.severity, counter.read_modify_write) == ("RACE001", Severity.HIGH, True)
    assert (results.rule_id, results.severity) == ("RACE002", Severity.MEDIUM)
    assert counter.spawn == "main -> Thread(bump)"


def test_lock_protected_and_caller_held_lock_are_clean(tmp_path):
    assert not races(tmp_path, {
        "app.py": """
            import threading
            TOTAL = 0
            SEEN = []
            _lock = threading.Lock()

            def protected():
                global TOTAL
                with _lock:
                    TOTAL += 1

            def helper():
                SEEN.append(1)

            def caller_holds_lock():
                with _lock:
                    helper()

            def main():
                threading.Thread(target=protected).start()
                threading.Thread(target=caller_holds_lock).start()
        """
    })


def test_a_fresh_lock_per_call_protects_nothing(tmp_path):
    found = races(tmp_path, {
        "app.py": """
            import threading
            TOTAL = 0

            def worker():
                global TOTAL
                with threading.Lock():
                    TOTAL += 1

            threading.Thread(target=worker).start()

            def main():
                threading.Thread(target=worker).start()
        """
    })
    assert ("worker", "TOTAL") in found


def test_instance_state_from_executor_and_thread_subclass(tmp_path):
    found = races(tmp_path, {
        "app.py": """
            import threading
            from concurrent.futures import ThreadPoolExecutor

            SEEN = 0

            class Counter:
                def __init__(self):
                    self.n = 0
                    self.items = []

                def incr(self):
                    self.n += 1

                def log(self, x):
                    self.items.append(x)

                def run(self):
                    with ThreadPoolExecutor() as pool:
                        pool.submit(self.incr)
                        pool.submit(self.log, 1)

            class Worker(threading.Thread):
                def run(self):
                    global SEEN
                    SEEN = SEEN + 1
        """
    })
    assert found[("incr", "self.n")].severity is Severity.HIGH  # augmented assignment
    assert found[("log", "self.items")].severity is Severity.LOW  # plain append, not read first
    assert found[("run", "SEEN")].read_modify_write  # SEEN read in its own new value
    assert ("__init__", "self.n") not in found  # construction happens before the object is shared


def test_unthreaded_code_and_thread_locals_are_not_flagged(tmp_path):
    assert not races(tmp_path, {
        "app.py": """
            import threading
            COUNT = 0
            _tls = threading.local()

            def sequential():
                global COUNT
                COUNT += 1

            def uses_tls():
                _tls.value = 1

            def main():
                sequential()
                threading.Thread(target=uses_tls).start()
        """
    })


def test_check_then_act_on_instance_state_is_high(tmp_path):
    found = races(tmp_path, {
        "stock.py": """
            from concurrent.futures import ThreadPoolExecutor

            class Stock:
                def __init__(self):
                    self.level = {}

                def take(self, sku):
                    current = self.level.get(sku, 0)
                    self.level[sku] = current - 1

            def run(stock):
                with ThreadPoolExecutor() as pool:
                    pool.map(stock.take, ["a", "b"])
        """
    })
    assert found[("take", "self.level")].severity is Severity.HIGH


# --- Exceptions from the sandbox ------------------------------------------------------

CALC = {
    "app/api.py": """
        from app import calc
        def handle(n):
            return calc.ratio(n)
        if __name__ == "__main__":
            handle(1)
    """,
    "app/calc.py": """
        def ratio(n):
            return 100 // n
    """,
    "tests/test_calc.py": "from app import calc\ndef test_a():\n    assert calc.ratio(0)\n",
}


def frames(*specs):
    return [StackFrame(file=f, line=ln, function=fn) for f, ln, fn in specs]


def sandbox(tests=(), probes=()):
    return SandboxRunnerOutput(isolation=Isolation.SUBPROCESS, tests=list(tests), probes=list(probes))


def case(node_id, exc_type, message, stack, outcome=CaseOutcome.FAILED):
    return CaseResult(node_id=node_id, outcome=outcome, exception=ExceptionInfo(type=exc_type, message=message, frames=stack))


def sentinel_input(tmp_path, files, sb):
    repo, _, graph = build(tmp_path, files)
    return DiagnosticSentinelInput(root=tmp_path, repo=repo, sandbox=sb, graph=graph)


def only(out, source):
    return [f for f in out.findings if f.source is source]


def test_same_failure_across_tests_is_one_finding_at_the_innermost_app_frame(tmp_path):
    stack = frames(("tests/test_calc.py", 3, "tests.test_calc.test_a"), ("app/calc.py", 2, "app.calc.ratio"))
    sb = sandbox(
        tests=[
            case("tests/test_calc.py::test_a", "ZeroDivisionError", "integer division or modulo by zero", stack),
            case("tests/test_calc.py::test_b", "ZeroDivisionError", "integer division or modulo by zero", stack),
            case("tests/test_calc.py::test_c", "ZeroDivisionError", "integer division or modulo by zero", stack),
            CaseResult(node_id="tests/test_calc.py::test_ok", outcome=CaseOutcome.PASSED),
        ]
    )
    out = diagnose(sentinel_input(tmp_path, CALC, sb))
    (finding,) = only(out, FindingSource.SANDBOX)

    assert (finding.category, finding.rule_id, finding.severity) == (FindingCategory.EXCEPTION, "EXC-ZeroDivisionError", Severity.HIGH)
    assert (finding.location.file, finding.location.line) == ("app/calc.py", 2)
    assert finding.node_id == "fn:app.calc.ratio"
    assert "2 more" in finding.evidence and finding.source_test == "tests/test_calc.py::test_a"
    assert finding.exception and [f.function for f in finding.exception.frames][-1] == "app.calc.ratio"


def test_assertions_guard_violations_and_sql_syntax_errors_are_rated_apart(tmp_path):
    sb = sandbox(
        tests=[
            case("t::assertion", "AssertionError", "assert 1 == 2", frames(("tests/test_calc.py", 3, "tests.test_calc.test_a"))),
            case("t::guard", "SandboxViolation", "network access is blocked", frames(("app/calc.py", 2, "app.calc.ratio"))),
            case("t::sql", "OperationalError", 'near "Brien": syntax error', frames(("app/calc.py", 1, "app.calc.ratio"))),
        ],
        probes=[
            SyntheticProbe(
                target="app.api.handle", inputs="(0,)", outcome=CaseOutcome.ERROR,
                exception=ExceptionInfo(type="KeyError", message="'x'", frames=frames(("app/api.py", 3, "app.api.handle"))),
            )
        ],
    )
    out = diagnose(sentinel_input(tmp_path, CALC, sb))
    by_rule = {f.rule_id: f for f in only(out, FindingSource.SANDBOX)}

    assert by_rule["EXC-AssertionError"].severity is Severity.MEDIUM
    assert by_rule["EXC-AssertionError"].node_id is None  # raised in a test, which is not in the graph
    assert by_rule["EXC-SandboxViolation"].severity is Severity.LOW
    sql = by_rule["EXC-OperationalError"]
    assert (sql.owasp, sql.owasp_name) == ("A03:2021", "Injection") and "SQL" in sql.title
    assert by_rule["EXC-KeyError"].source_test == "app.api.handle((0,))"


# --- Bandit and OWASP -----------------------------------------------------------------

DANGEROUS = {
    "app/danger.py": """
        import hashlib
        import pickle
        import subprocess

        PASSWORD = "hunter2hunter2"

        def run(cmd):
            subprocess.call(cmd, shell=True)

        def load(blob):
            return pickle.loads(blob)

        def digest(x):
            return hashlib.md5(x).hexdigest()

        def calc(expr):
            return eval(expr)

        def lookup(conn, name):
            return conn.execute("SELECT * FROM t WHERE n = '%s'" % name)

        def check(x):
            assert x
    """,
    "tests/test_danger.py": "def test_x():\n    assert True\n",
}


def test_bandit_results_map_to_owasp_categories_and_rated(tmp_path):
    out = diagnose(sentinel_input(tmp_path, DANGEROUS, None))
    found = {f.rule_id: f for f in only(out, FindingSource.BANDIT)}

    assert {r: f.owasp for r, f in found.items()} == {
        "B105": "A07:2021", "B602": "A03:2021", "B301": "A08:2021",
        "B324": "A02:2021", "B307": "A03:2021", "B608": "A03:2021",
    }  # no B101/B403/B404 (notes, not weaknesses), nothing from tests/
    assert found["B602"].severity is Severity.CRITICAL  # shell=True with a variable command
    assert found["B608"].severity is Severity.MEDIUM  # bandit is unsure (LOW), floored because it is injection
    assert found["B324"].severity is Severity.HIGH  # not an injection, so never critical
    assert found["B602"].owasp_name == "Injection" and found["B602"].category is FindingCategory.SECURITY
    assert found["B307"].node_id == "fn:app.danger.calc"
    assert next(r for r in out.sources if r.source is FindingSource.BANDIT).findings == 6


def test_findings_are_deduplicated_sorted_and_have_stable_ids(tmp_path):
    first = diagnose(sentinel_input(tmp_path / "a", DANGEROUS, None))
    second = diagnose(sentinel_input(tmp_path / "b", DANGEROUS, None))
    assert [f.id for f in first.findings] == [f.id for f in second.findings]
    assert len({f.id for f in first.findings}) == len(first.findings)
    ranks = [list(Severity).index(f.severity) for f in first.findings]
    assert ranks == sorted(ranks, reverse=True)  # most severe first (Severity is declared low -> critical)


# --- Source handling -------------------------------------------------------------------


def test_a_failing_source_is_reported_without_hiding_the_others(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise BanditError("bandit exploded")

    monkeypatch.setattr("app.agents.diagnostic_sentinel.run_bandit", broken)
    out = diagnose(sentinel_input(tmp_path, CALC, None))
    reports = {r.source: r for r in out.sources}

    assert reports[FindingSource.BANDIT].status is SourceStatus.FAILED and "exploded" in reports[FindingSource.BANDIT].detail
    assert reports[FindingSource.SANDBOX].status is SourceStatus.SKIPPED
    assert reports[FindingSource.STATIC].status is SourceStatus.OK


@pytest.mark.anyio
async def test_stage_fails_only_when_no_source_could_run(tmp_path, monkeypatch):
    inp = sentinel_input(tmp_path, CALC, None)

    def broken(*args, **kwargs):
        raise BanditError("no")

    monkeypatch.setattr("app.agents.diagnostic_sentinel.run_bandit", broken)
    monkeypatch.setattr("app.agents.diagnostic_sentinel.find_races", broken)

    class Log:
        def info(self, m): pass
        warning = error = debug = info

    with pytest.raises(RuntimeError, match="no diagnostic source could run"):
        await DiagnosticSentinel().run(inp, Log())

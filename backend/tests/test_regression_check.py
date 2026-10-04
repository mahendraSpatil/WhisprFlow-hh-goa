from __future__ import annotations

import sys
from pathlib import Path

import pytest
from helpers import Log, build, make_diff

from app.agents.diagnostic_sentinel import diagnose
from app.agents.regression_check import RegressionCheck, check, compare_findings, compare_tests
from app.models.agents import (
    CaseOutcome,
    CaseResult,
    DiagnosticSentinelInput,
    Finding,
    FindingCategory,
    FindingSource,
    Isolation,
    Patch,
    PatchedFile,
    PatchStatus,
    RegressionCheckInput,
    SandboxRunnerOutput,
    Severity,
    SourceLocation,
    Symbol,
    SymbolKind,
    Verdict,
)
from app.sandbox import Sandbox, fresh_copy, run_suite

CALC = "def ratio(n):\n    return 100 // n\n\n\ndef double(n):\n    return n * 2\n"
FILES = {
    "shop/__init__.py": "",
    "shop/calc.py": CALC,
    "tests/test_calc.py": (
        "from shop import calc\n\n\n"
        "def test_ratio_of_zero_is_zero():\n    assert calc.ratio(0) == 0\n\n\n"
        "def test_ratio_of_ten():\n    assert calc.ratio(10) == 10\n\n\n"
        "def test_double():\n    assert calc.double(2) == 4\n"
    ),
}
GOOD_FIX = CALC.replace("return 100 // n", "return 100 // n if n else 0")
OTHER_FIX = CALC.replace("return 100 // n", "return 100 // n if n > 0 else 0")  # fixes the same line differently


def patch(pid: str, path: str, old: str, new: str) -> Patch:
    return Patch(
        id=pid, root_cause_id=f"RC-{pid}", status=PatchStatus.VALID, diff=make_diff(path, old, new),
        files=[PatchedFile(path=path, original=old, patched=new)],
    )


def make_input(tmp_path: Path, files: dict[str, str], patches: list[Patch], *, with_baseline: bool = True) -> RegressionCheckInput:
    """What the pipeline would hand RegressionCheck: the baseline test run and findings, measured the usual way."""
    root = tmp_path / "workspace"
    repo, system, graph = build(root, files)
    work = tmp_path / "work"
    baseline = findings = None
    if with_baseline:
        sandbox = Sandbox(Path(sys.executable), Isolation.SUBPROCESS)
        baseline = run_suite(sandbox, fresh_copy(root, work / "baseline" / "repo"), ["tests"], Log())
        findings = diagnose(DiagnosticSentinelInput(root=root, repo=repo, sandbox=baseline, graph=graph)).findings
    return RegressionCheckInput(
        root=root, work_dir=work, repo=repo, patches=patches, findings=findings, dependencies=[],
        test_runner="pytest", test_paths=["tests"], baseline=baseline,
    )


def by_id(output):
    return {v.patch_id: v for v in output.verdicts}


# --- Verdicts ---------------------------------------------------------------------------------


def test_a_good_patch_is_accepted_and_what_it_fixed_is_measured(tmp_path):
    inp = make_input(tmp_path, FILES, [patch("good", "shop/calc.py", CALC, GOOD_FIX)])
    assert inp.baseline.totals.passed == 2 and inp.baseline.totals.failed == 1  # the division by zero
    assert [f.rule_id for f in inp.findings] == ["EXC-ZeroDivisionError"]

    out = check(inp, Log())
    verdict = by_id(out)["good"]
    assert (verdict.verdict, verdict.accepted, verdict.reasons) == (Verdict.PASS, True, [])
    assert verdict.newly_passing == ["tests/test_calc.py::test_ratio_of_zero_is_zero"] and verdict.newly_failing == []
    assert [(r.rule_id, r.location.file) for r in verdict.findings_resolved] == [("EXC-ZeroDivisionError", "shop/calc.py")]
    assert verdict.findings_resolved[0].finding_id == inp.findings[0].id  # refers to the finding DiagnosticSentinel reported
    assert verdict.totals.passed == 3 and verdict.totals.failed == 0

    s = out.summary
    assert s.patches_applied == ["good"]
    assert (s.tests_before.passed, s.tests_before.failed, s.tests_after.passed, s.tests_after.failed) == (2, 1, 3, 0)
    assert s.tests_fixed == ["tests/test_calc.py::test_ratio_of_zero_is_zero"] and s.tests_broken == []
    assert (s.findings_before, s.findings_after) == (1, 0) and len(s.findings_resolved) == 1 and s.findings_introduced == []
    assert out.baseline_totals == inp.baseline.totals


def test_a_patch_that_breaks_a_test_or_adds_a_finding_is_rejected_with_the_reason(tmp_path):
    breaks = CALC.replace("return n * 2", "return n * 3")
    leaks = 'API_PASSWORD = "hunter2hunter2"\n\n\n' + CALC
    stale = CALC.replace("100 //", "100 /")  # context that is not in the file
    patches = [
        patch("good", "shop/calc.py", CALC, GOOD_FIX),
        patch("breaks", "shop/calc.py", CALC, breaks),
        patch("leaks", "shop/calc.py", CALC, leaks),
        patch("stale", "shop/calc.py", stale, GOOD_FIX),
    ]
    out = check(make_input(tmp_path, FILES, patches), Log())
    v = by_id(out)

    assert v["breaks"].verdict is Verdict.REGRESSED and not v["breaks"].accepted
    assert v["breaks"].newly_failing == ["tests/test_calc.py::test_double"]
    assert v["breaks"].reasons == ["breaks 1 test that passed before: tests/test_calc.py::test_double"]

    assert v["leaks"].verdict is Verdict.REGRESSED and not v["leaks"].accepted
    assert [f.rule_id for f in v["leaks"].findings_introduced] == ["B105"]
    assert "introduces 1 new finding: B105 at shop/calc.py:1" in v["leaks"].reasons[0]

    assert v["stale"].verdict is Verdict.APPLY_FAILED and v["stale"].reasons[0].startswith("does not apply to a fresh copy of the repo")
    assert "patch does not apply" in v["stale"].detail  # git's full message

    # only the good patch survives into the combined result
    assert v["good"].accepted and out.summary.patches_applied == ["good"]
    assert out.summary.tests_broken == [] and out.summary.findings_introduced == []


def test_conflicting_and_duplicate_patches(tmp_path):
    x, y = patch("x", "shop/calc.py", CALC, GOOD_FIX), patch("y", "shop/calc.py", CALC, OTHER_FIX)
    dup = patch("dup", "shop/calc.py", CALC, GOOD_FIX)  # the same change as x
    out = check(make_input(tmp_path, FILES, [x, y, dup]), Log())
    v = by_id(out)

    assert all(v[p].newly_passing for p in "xy") and v["dup"].accepted  # each fixes the failing test alone
    assert v["x"].accepted and v["x"].reasons == []
    assert v["y"].verdict is Verdict.APPLY_FAILED and not v["y"].accepted
    assert "does not apply on top of the other accepted patches" in v["y"].reasons[0]
    assert v["dup"].accepted and "already made by an earlier accepted patch" in v["dup"].reasons[0]
    assert out.summary.patches_applied == ["x"] and out.summary.tests_after.failed == 0


def test_patches_that_are_fine_alone_but_break_in_combination(tmp_path):
    files = {
        "shop/__init__.py": "",
        "shop/a.py": "VALUE = 1\n",
        "shop/b.py": "VALUE = 1\n",
        "tests/test_either.py": "from shop import a, b\n\n\ndef test_one_of_them_is_one():\n    assert a.VALUE == 1 or b.VALUE == 1\n",
    }
    p1, p2 = patch("p1", "shop/a.py", "VALUE = 1\n", "VALUE = 5\n"), patch("p2", "shop/b.py", "VALUE = 1\n", "VALUE = 5\n")
    out = check(make_input(tmp_path, files, [p1, p2]), Log())
    v = by_id(out)

    assert v["p1"].accepted and v["p1"].reasons == ["applies cleanly, but no failing test and no finding changed"]
    assert not v["p2"].accepted and v["p2"].verdict is Verdict.REGRESSED
    assert v["p2"].reasons[0].startswith("breaks only in combination with p1: breaks 1 test that passed before")
    assert out.summary.patches_applied == ["p1"] and out.summary.tests_broken == []  # the final state is the clean one


def test_baseline_is_measured_when_the_pipeline_did_not_provide_it(tmp_path):
    inp = make_input(tmp_path, FILES, [patch("good", "shop/calc.py", CALC, GOOD_FIX)], with_baseline=False)
    assert inp.baseline is None and inp.findings is None
    out = check(inp, Log())
    assert by_id(out)["good"].accepted and out.summary.tests_fixed and out.summary.findings_resolved
    assert out.baseline_totals.failed == 1


@pytest.mark.anyio
async def test_the_agent_verifies_only_valid_patches_and_reports_when_there_are_none(tmp_path):
    inp = make_input(tmp_path, FILES, [])
    out = await RegressionCheck().run(inp, Log())
    assert out.verdicts == [] and out.summary is None and out.baseline_totals == inp.baseline.totals

    log = Log()
    out = await RegressionCheck().run(make_input(tmp_path / "again", FILES, [patch("good", "shop/calc.py", CALC, GOOD_FIX)]), log)
    assert any("good: accepted" in line for line in log.lines) and any("Together:" in line for line in log.lines)


# --- Comparing --------------------------------------------------------------------------------------


def case(node_id, outcome):
    return CaseResult(node_id=node_id, outcome=outcome)


def run_of(**outcomes):
    return SandboxRunnerOutput(
        isolation=Isolation.SUBPROCESS, tests=[case(n, CaseOutcome(o)) for n, o in outcomes.items()]
    )


def test_test_comparison():
    before = run_of(a="passed", b="failed", c="error", d="passed", e="skipped", f="passed")
    after = run_of(a="passed", b="passed", c="failed", d="failed", e="passed", g="error")  # f vanished, g is new and failing
    fixed, broken = compare_tests(before, after)
    assert fixed == ["b"]  # c is still broken; e was skipped
    assert sorted(broken) == ["d", "f", "g"]
    assert compare_tests(None, after) == ([], []) and compare_tests(before, None) == ([], [])


def finding(rule, file, line):
    return Finding(
        id=f"{rule}:{file}:{line}", category=FindingCategory.SECURITY, source=FindingSource.BANDIT, rule_id=rule, title=rule,
        severity=Severity.MEDIUM, location=SourceLocation(file=file, line=line), evidence="",
    )


def func(qualname, file, start, end):
    return Symbol(
        qualname=qualname, name=qualname.rsplit(".", 1)[-1], kind=SymbolKind.FUNCTION, module=file[:-3],
        location=SourceLocation(file=file, line=start, end_line=end),
    )


def test_findings_keep_their_identity_when_edits_move_them():
    symbols_before = [func("m.f", "m.py", 10, 20), func("m.g", "m.py", 30, 40)]
    symbols_after = [func("m.f", "m.py", 13, 23), func("m.g", "m.py", 33, 43)]  # a patch added three lines above
    before = [finding("B608", "m.py", 15), finding("B105", "m.py", 35)]
    after = [finding("B608", "m.py", 18), finding("B324", "m.py", 36)]  # B608 moved with its function; B105 gone; B324 new

    resolved, introduced = compare_findings(before, symbols_before, after, symbols_after)
    assert [f.rule_id for f in resolved] == ["B105"] and [f.rule_id for f in introduced] == ["B324"]


def test_findings_are_compared_by_count_per_function():
    symbols = [func("m.f", "m.py", 1, 50)]
    two = [finding("B608", "m.py", 5), finding("B608", "m.py", 9)]
    resolved, introduced = compare_findings(two, symbols, two[:1], symbols)
    assert len(resolved) == 1 and introduced == []
    resolved, introduced = compare_findings(two[:1], symbols, two, symbols)
    assert resolved == [] and len(introduced) == 1
    assert compare_findings(two, symbols, two, symbols) == ([], [])


# --- End to end on demo_target ---------------------------------------------------------------------

DEMO_TARGET = Path(__file__).resolve().parents[2] / "demo_target"


@pytest.mark.skipif(not DEMO_TARGET.is_dir(), reason="demo_target/ not present")
def test_demo_target_end_to_end_the_patches_are_verified_and_nodes_turn_fixed(tmp_path):
    from fastapi.testclient import TestClient
    from test_patch_master import scripted_fixes
    from helpers import FakeClient

    from app.agents import default_pipeline
    from app.agents.patch_master import PatchMaster
    from app.main import create_app
    from app.models.run import StageName
    from app.orchestrator.runner import Orchestrator

    files = {rel: (DEMO_TARGET / rel).read_text() for rel in ("orders/db.py", "orders/service.py")}
    agents = [PatchMaster(client=FakeClient(scripted_fixes(files))) if a.name is StageName.PATCH_MASTER else a for a in default_pipeline()]
    app = create_app(Orchestrator(workspace_root=tmp_path / "runs", agents=agents))
    with TestClient(app) as client:
        run_id = client.post("/runs", json={"source": str(DEMO_TARGET)}).json()["run_id"]
        with client.websocket_connect(f"/ws/runs/{run_id}") as ws:
            while True:
                event = ws.receive_json()
                if event["type"] == "run.status" and event["status"] in ("completed", "completed_with_escalations", "failed"):
                    assert event["status"] == "completed", event
                    break

        regression = client.get(f"/runs/{run_id}/regression").json()
        graph = client.get(f"/runs/{run_id}/graph").json()
        assert client.get("/runs/nope/regression").status_code == 404

    verdicts = {v["patch_id"]: v for v in regression["verdicts"]}
    assert len(verdicts) == 4 and all(v["accepted"] and v["verdict"] == "pass" for v in verdicts.values())
    assert any("already made by an earlier accepted patch" in r for v in verdicts.values() for r in v["reasons"])  # the two SQL fixes are identical

    s = regression["summary"]
    assert (s["tests_before"]["passed"], s["tests_before"]["failed"]) == (17, 7)
    assert (s["tests_after"]["passed"], s["tests_after"]["failed"]) == (24, 0)
    assert len(s["tests_fixed"]) == 7 and s["tests_broken"] == [] and s["findings_introduced"] == []
    assert (s["findings_before"], s["findings_after"]) == (7, 0)
    assert {f["rule_id"] for f in s["findings_resolved"]} >= {"B608", "RACE003", "EXC-ZeroDivisionError", "EXC-OperationalError"}

    # The nodes holding the three planted bugs are fixed, and the metric still says what was found there.
    nodes = {n["id"]: n for n in graph["nodes"]}
    for node_id in ("fn:orders.service.Inventory.reserve", "fn:orders.db.find_orders_by_customer", "fn:orders.service.OrderService.summarize"):
        assert nodes[node_id]["status"] == "fixed" and nodes[node_id]["metrics"]["findings"] >= 1
    assert not [n for n in graph["nodes"] if n["status"] in ("failing", "warning")]

    # MemoryKeeper wrote the run down: the four findings in application code, each with its root cause,
    # accepted patch and verdict. The three assertion failures in test code are not remembered.
    import asyncio

    from app.graph_overlay import overlay_findings
    from app.memory import MemoryStore, default_path
    from app.models.agents import NodeStatus, SourceKind

    stored = MemoryStore(default_path()).list_incidents()
    assert {i.rule_id for i in stored} == {"B608", "RACE003", "EXC-ZeroDivisionError", "EXC-OperationalError"}
    assert all(i.run_id == run_id and Path(i.source) == DEMO_TARGET for i in stored)
    assert all(i.patch_status == "valid" and i.regression_passed is True and i.patch_diff and i.root_cause for i in stored)
    assert {i.kind for i in stored} == {"B608", "RACE003", "ZeroDivisionError", "OperationalError"}

    # A second run over the same repo recognizes every one of those patterns and rates them higher.
    keep = {StageName.REPO_SCOUT, StageName.SYSTEM_ANALYST, StageName.PIPELINE_ARCHITECT, StageName.SANDBOX_RUNNER, StageName.DIAGNOSTIC_SENTINEL}

    async def second_run():
        again = Orchestrator(workspace_root=tmp_path / "runs-again", agents=[a for a in default_pipeline() if a.name in keep])
        ctx2, events2 = again.create_run(SourceKind.LOCAL, str(DEMO_TARGET))
        await again.execute(ctx2, events2)
        return ctx2

    second = asyncio.run(second_run())
    signed = {f.rule_id: f for f in second.diagnostic_sentinel.findings if f.signature}
    assert set(signed) == {"B608", "RACE003", "EXC-ZeroDivisionError", "EXC-OperationalError"}
    assert all(f.seen_before == 1 for f in signed.values())
    assert (signed["B608"].severity, signed["B608"].base_severity) == (Severity.HIGH, Severity.MEDIUM)
    assert signed["RACE003"].severity is Severity.CRITICAL and signed["RACE003"].base_severity is Severity.HIGH
    assert all(f.seen_before == 0 for f in second.diagnostic_sentinel.findings if not f.signature)

    nodes_again = {n.id: n for n in overlay_findings(second.pipeline_architect.graph, second.diagnostic_sentinel.findings).nodes}
    reserve = nodes_again["fn:orders.service.Inventory.reserve"]
    assert reserve.metrics.seen_before == 1 and reserve.status is NodeStatus.FAILING  # not fixed yet in this run
    assert nodes_again["fn:orders.db.find_orders_by_customer"].status is NodeStatus.FAILING  # B608 went medium -> high

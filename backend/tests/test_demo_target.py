"""RepoScout and SystemAnalyst against the demo_target app in this monorepo."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.agents.repo_scout import RepoScout
from app.agents.system_analyst import SystemAnalyst
from app.models.agents import ArchitectureStyle, CallResolution, Layer, SourceKind
from app.models.run import RunStatus
from app.orchestrator.runner import Orchestrator

DEMO_TARGET = Path(__file__).resolve().parents[2] / "demo_target"

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not DEMO_TARGET.is_dir(), reason="demo_target/ not present"),
]


@pytest.fixture
async def ctx(tmp_path):
    orchestrator = Orchestrator(workspace_root=tmp_path, agents=[RepoScout(), SystemAnalyst()])
    ctx, events = orchestrator.create_run(SourceKind.LOCAL, str(DEMO_TARGET))
    await orchestrator.execute(ctx, events)
    assert ctx.status is RunStatus.COMPLETED
    return ctx


async def test_index_covers_the_app(ctx):
    repo = ctx.repo_scout
    assert {m.name for m in repo.modules if not m.is_test} == {"orders", "orders.api", "orders.service", "orders.db"}
    assert repo.parse_errors == []
    reserve = next(s for s in repo.symbols if s.qualname == "orders.service.Inventory.reserve")
    assert (reserve.location.file, reserve.location.line) == ("orders/service.py", 67)


async def test_call_graph_reaches_every_planted_bug_site(ctx):
    edges = {(c.caller, c.target) for c in ctx.repo_scout.calls if c.resolution is CallResolution.INTERNAL}
    # API -> service -> data and inventory, through attributes typed in __init__
    assert ("orders.api.OrderAPI.create_order", "orders.service.OrderService.place_order") in edges
    assert ("orders.api.OrderAPI.get_order", "orders.service.OrderService.summarize") in edges
    assert ("orders.api.OrderAPI.list_orders", "orders.service.OrderService.orders_for") in edges
    assert ("orders.service.OrderService.orders_for", "orders.db.find_orders_by_customer") in edges
    assert ("orders.service.OrderService.place_batch.try_reserve", "orders.service.Inventory.reserve") in edges
    assert ("orders.service.Inventory.reserve", "orders.service.WarehouseClient.confirm_pick") in edges

    external = {(c.caller, c.target) for c in ctx.repo_scout.calls if c.resolution is CallResolution.EXTERNAL}
    assert ("orders.db.find_orders_by_customer", "sqlite3.Connection.execute") in external
    assert ("orders.service.OrderService.place_batch", "concurrent.futures.ThreadPoolExecutor.map") in external


async def test_system_analysis(ctx):
    system = ctx.system_analyst
    assert {c.key for c in system.stack} == {"concurrent.futures", "threading", "sqlite3", "pytest", "wsgiref"}
    assert {l.module: l.layer for l in system.layers if l.layer is not Layer.TEST} == {
        "orders": Layer.UTIL,
        "orders.api": Layer.API,
        "orders.service": Layer.SERVICE,
        "orders.db": Layer.DATA,
    }
    assert system.architecture is ArchitectureStyle.WEB_SERVICE
    assert system.test_runner == "pytest" and system.test_paths == ["tests"]
    assert system.summary.startswith("Web service on wsgiref (stdlib WSGI) with SQLite storage.")


# --- Diagnosis (full pipeline, no network: the test default disables Claude) ----------


@pytest.fixture
async def diagnosed(tmp_path):
    orchestrator = Orchestrator(workspace_root=tmp_path)
    ctx, events = orchestrator.create_run(SourceKind.LOCAL, str(DEMO_TARGET))
    await orchestrator.execute(ctx, events)
    assert ctx.status is RunStatus.COMPLETED
    return ctx


async def test_diagnosis_finds_all_three_planted_bugs(diagnosed):
    from app.models.agents import Severity

    findings = {(f.rule_id, f.location.file): f for f in diagnosed.diagnostic_sentinel.findings}
    race = findings[("RACE003", "orders/service.py")]
    assert race.severity is Severity.HIGH and race.node_id == "fn:orders.service.Inventory.reserve"
    assert "ThreadPoolExecutor.map" in race.evidence and "self._stock" in race.evidence

    sql = findings[("B608", "orders/db.py")]
    assert (sql.owasp, sql.owasp_name, sql.severity) == ("A03:2021", "Injection", Severity.MEDIUM)
    assert sql.node_id == "fn:orders.db.find_orders_by_customer"
    # The division by zero is only visible at runtime: it comes from SandboxRunner's failing tests.
    zero = findings[("EXC-ZeroDivisionError", "orders/service.py")]
    assert zero.severity is Severity.HIGH and zero.node_id == "fn:orders.service.OrderService.summarize"
    assert zero.exception.frames[-1].function == "orders.service.OrderService.summarize"
    # Also: the same SQL bug seen as a failing query, and three test-side assertion failures with no graph node.
    assert findings[("EXC-OperationalError", "orders/db.py")].owasp == "A03:2021"
    every = diagnosed.diagnostic_sentinel.findings  # the dict above collapses findings that share a rule and file
    assert sum(f.rule_id == "EXC-AssertionError" for f in every) == 3
    assert len(every) == 7


async def test_root_causes_trace_from_the_entry_node_to_the_offending_line(diagnosed):
    causes = {(c.location.file, c.location.line): c for c in diagnosed.root_cause_diagnostician.root_causes}

    race = causes[("orders/service.py", 73)]
    assert [s.label for s in race.chain] == [
        "orders.api __main__", "make_wsgi_app", "make_wsgi_app.app", "OrderAPI.create_order", "OrderService.place_order", "Inventory.reserve",
    ]
    assert (race.chain[-1].role.value, race.chain[-1].line) == ("offender", 73)
    assert race.snippet.highlight_line == 73 and "self._stock[sku] = current - quantity" in race.snippet.code

    sql = causes[("orders/db.py", 53)]
    assert [s.label for s in sql.chain][-3:] == ["OrderAPI.list_orders", "OrderService.orders_for", "find_orders_by_customer"]
    assert sql.explanation_source.value == "template"  # CODELOOP_LLM=off in tests

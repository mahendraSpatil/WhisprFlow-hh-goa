from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from app.agents.indexer import index_repository
from app.agents.pipeline_architect import COLUMN_WIDTH, build_graph
from app.agents.system_analyst import analyze
from app.models.agents import (
    EdgeKind,
    Layer,
    NodeStatus,
    NodeType,
    PipelineArchitectInput,
    RepoScoutOutput,
    SourceKind,
    SystemAnalystInput,
)

DEMO_TARGET = Path(__file__).resolve().parents[2] / "demo_target"


def graph_for(root: Path, include_tests: bool = False):
    index = index_repository(root, {"__pycache__", ".pytest_cache"}, 1_000_000)
    repo = RepoScoutOutput(
        root=root, source_kind=SourceKind.LOCAL, modules=index.modules, symbols=index.symbols, calls=index.calls
    )
    system = analyze(SystemAnalystInput(root=root, repo=repo))
    return build_graph(PipelineArchitectInput(repo=repo, system=system, include_tests=include_tests))


@pytest.fixture(scope="module")
def demo_graph():
    if not DEMO_TARGET.is_dir():
        pytest.skip("demo_target/ not present")
    return graph_for(DEMO_TARGET)


def test_node_schema_and_types(demo_graph):
    nodes = {n.id: n for n in demo_graph.nodes}

    reserve = nodes["fn:orders.service.Inventory.reserve"]
    assert (reserve.label, reserve.type, reserve.layer) == ("Inventory.reserve", NodeType.SERVICE, Layer.SERVICE)
    assert (reserve.file, reserve.start_line, reserve.end_line) == ("orders/service.py", 67, 74)
    assert reserve.status is NodeStatus.IDLE
    assert reserve.metrics.loc == 8 and reserve.metrics.complexity == 2
    assert reserve.metrics.fan_in == 2  # place_order and the try_reserve closure

    assert nodes["main:orders.api"].type is NodeType.ENTRY
    assert nodes["fn:orders.db.find_orders_by_customer"].type is NodeType.FUNCTION
    assert nodes["res:sqlite3"].type is NodeType.DATABASE and nodes["res:sqlite3"].label == "SQLite"
    assert nodes["res:concurrent.futures"].type is NodeType.EXTERNAL
    # tests, classes and uninteresting libraries are not nodes
    assert not any(i.startswith("fn:tests.") for i in nodes)
    assert "fn:orders.service.OrderService" not in nodes and "res:json" not in nodes


def test_edges(demo_graph):
    edges = {(e.source, e.target): e for e in demo_graph.edges}

    def edge(source: str, target: str):
        return edges[(source, target)]

    query = edge("fn:orders.db.find_orders_by_customer", "res:sqlite3")
    assert query.kind is EdgeKind.DATA and query.animated
    assert query.id == "data:fn:orders.db.find_orders_by_customer->res:sqlite3"

    call = edge("fn:orders.api.OrderAPI.list_orders", "fn:orders.service.OrderService.orders_for")
    assert call.kind is EdgeKind.CALL and not call.animated
    # the WSGI closure dispatches to the handlers through its typed `api` parameter
    assert edge("fn:orders.api.make_wsgi_app.app", "fn:orders.api.OrderAPI.get_order").kind is EdgeKind.CALL
    assert edge("fn:orders.api.make_wsgi_app.app", "fn:orders.api.OrderAPI.create_order").kind is EdgeKind.IMPORT
    # a callback handed to the thread pool
    assert edge("fn:orders.service.OrderService.place_batch", "fn:orders.service.OrderService.place_batch.try_reserve").kind is EdgeKind.IMPORT
    assert edge("fn:orders.service.OrderService.place_batch", "res:concurrent.futures").kind is EdgeKind.CALL
    # constructing a class is a call to its __init__
    assert edge("fn:orders.api.build_api", "fn:orders.service.OrderService.__init__").kind is EdgeKind.CALL
    # code under `if __name__ == "__main__"` hangs off the entry node
    assert edge("main:orders.api", "res:wsgiref").kind is EdgeKind.CALL

    node_ids = {n.id for n in demo_graph.nodes}
    assert all(e.source in node_ids and e.target in node_ids for e in demo_graph.edges)
    assert len({e.id for e in demo_graph.edges}) == len(demo_graph.edges)


def test_columns_read_left_to_right_by_layer(demo_graph):
    assert [c.key for c in demo_graph.columns] == ["entry", "api", "service", "data", "database", "external"]
    assert [c.x for c in demo_graph.columns] == [i * COLUMN_WIDTH for i in range(6)]

    x_of = {c.key: c.x for c in demo_graph.columns}
    for n in demo_graph.nodes:
        expected = {NodeType.DATABASE: "database", NodeType.EXTERNAL: "external"}.get(n.type) or n.layer.value
        assert n.position.x == x_of[expected], n.id

    # no two nodes overlap, and the layout starts at the top
    positions = [(n.position.x, n.position.y) for n in demo_graph.nodes]
    assert len(set(positions)) == len(positions)
    assert min(y for _, y in positions) == 0


def test_layout_keeps_callers_and_callees_aligned(tmp_path):
    # Two independent chains: each callee should sit level with its own caller.
    files = {
        "app/api.py": "from app import service\ndef a():\n    service.x()\ndef b():\n    service.y()\n",
        "app/service.py": "def y():\n    pass\ndef x():\n    pass\n",
    }
    for rel, content in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(textwrap.dedent(content))
    graph = graph_for(tmp_path)
    y = {n.label: n.position.y for n in graph.nodes}
    assert y["a"] == y["x"] and y["b"] == y["y"]


def test_tests_can_be_included(tmp_path):
    (tmp_path / "lib.py").write_text("def f():\n    return 1\n")
    (tmp_path / "test_lib.py").write_text("from lib import f\ndef test_f():\n    assert f() == 1\n")
    graph = graph_for(tmp_path, include_tests=True)
    test_node = next(n for n in graph.nodes if n.id == "fn:test_lib.test_f")
    assert test_node.layer is Layer.TEST and graph.columns[0].key == "entry"
    assert any(e.source == "fn:test_lib.test_f" and e.target == "fn:lib.f" for e in graph.edges)

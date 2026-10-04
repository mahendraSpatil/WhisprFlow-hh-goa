from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from dataclasses import dataclass

from app.agents.base import Agent
from app.agents.system_analyst import DATA_CATEGORIES, KNOWN_TECH, tech_key
from app.models.agents import (
    CallResolution,
    EdgeKind,
    EntrypointKind,
    Graph,
    GraphColumn,
    GraphEdge,
    GraphNode,
    Layer,
    NodeMetrics,
    NodeType,
    PipelineArchitectInput,
    PipelineArchitectOutput,
    Position,
    RepoScoutOutput,
    StackCategory,
    Symbol,
    SymbolKind,
    SystemAnalystOutput,
)
from app.models.run import RunContext, StageName
from app.orchestrator.events import StageLogger

COLUMN_WIDTH = 340.0
ROW_HEIGHT = 132.0  # card height in the UI is ~100px; leaves room for edges between rows
LAYOUT_SWEEPS = 4

# Left to right: where requests enter, down through the layers, into storage. Shared
# helpers and third-party libraries sit on the right so they don't split the main flow.
COLUMNS: list[tuple[str, str]] = [
    ("entry", "Entry"),
    ("api", "API"),
    ("service", "Service"),
    ("data", "Data access"),
    ("database", "Databases"),
    ("util", "Utilities"),
    ("external", "External"),
]
COLUMN_INDEX = {key: i for i, (key, _) in enumerate(COLUMNS)}

# Libraries worth a node: they move data in or out, or change how code runs. Everything
# else (json, re, dataclasses...) would only add noise to a data-flow view.
GRAPHED_CATEGORIES = DATA_CATEGORIES | {
    StackCategory.WEB_FRAMEWORK,
    StackCategory.WEB_SERVER,
    StackCategory.HTTP_CLIENT,
    StackCategory.TASK_QUEUE,
    StackCategory.CONCURRENCY,
}
# When two nodes are linked more than one way, the strongest relationship is drawn.
EDGE_PRIORITY = {EdgeKind.DATA: 3, EdgeKind.CALL: 2, EdgeKind.IMPORT: 1}


class PipelineArchitect(Agent[PipelineArchitectInput, PipelineArchitectOutput]):
    """Turns the symbol index and layers into a left-to-right data-flow graph."""

    name = StageName.PIPELINE_ARCHITECT
    title = "PipelineArchitect"
    output_model = PipelineArchitectOutput
    requires = (StageName.REPO_SCOUT, StageName.SYSTEM_ANALYST)
    default_timeout_s = 60.0

    def build_input(self, ctx: RunContext) -> PipelineArchitectInput:
        return PipelineArchitectInput(
            repo=ctx.require(StageName.REPO_SCOUT, RepoScoutOutput),
            system=ctx.require(StageName.SYSTEM_ANALYST, SystemAnalystOutput),
        )

    async def run(self, inp: PipelineArchitectInput, log: StageLogger) -> PipelineArchitectOutput:
        graph = await asyncio.to_thread(build_graph, inp)
        by_type = Counter(n.type.value for n in graph.nodes)
        by_kind = Counter(e.kind.value for e in graph.edges)
        log.info(
            f"Graph: {len(graph.nodes)} nodes ({_counts(by_type)}), {len(graph.edges)} edges ({_counts(by_kind)}), "
            f"columns: {' | '.join(c.title for c in graph.columns)}"
        )
        return PipelineArchitectOutput(graph=graph)


def _counts(counter: Counter[str]) -> str:
    return ", ".join(f"{n} {k}" for k, n in counter.most_common()) or "none"


def function_node_id(qualname: str) -> str:
    return f"fn:{qualname}"


def main_node_id(module: str) -> str:
    return f"main:{module}"


def resource_node_id(key: str) -> str:
    return f"res:{key}"


@dataclass
class _Draft:
    """A node before layout, plus the column it belongs in."""

    node: GraphNode
    column: int


def build_graph(inp: PipelineArchitectInput) -> Graph:
    repo, system = inp.repo, inp.system
    layer_of = {l.module: l.layer for l in system.layers}
    modules = {m.name: m for m in repo.modules}
    symbols = {s.qualname: s for s in repo.symbols}
    entry_symbols = {
        e.symbol
        for e in system.entrypoints
        if e.symbol and e.kind in (EntrypointKind.WEB_ROUTE, EntrypointKind.CLI_COMMAND)
    }
    graphed_tech = {c.key for c in system.stack if c.category in GRAPHED_CATEGORIES and c.used_in_app}

    def included(module: str) -> bool:
        return inp.include_tests or layer_of.get(module) is not Layer.TEST

    drafts: dict[str, _Draft] = {}

    # Function and method nodes. Classes are not nodes: constructing one is a call to its __init__.
    for s in repo.symbols:
        if s.kind is SymbolKind.CLASS or not included(s.module):
            continue
        layer = layer_of.get(s.module)
        if layer is Layer.TEST:
            column = COLUMN_INDEX["entry"]  # tests drive the code, so they sit where requests come in
        else:
            column = COLUMN_INDEX[layer.value if layer else "util"]
        drafts[function_node_id(s.qualname)] = _Draft(
            GraphNode(
                id=function_node_id(s.qualname),
                label=_label(s),
                type=_function_type(s, layer, entry_symbols),
                file=s.location.file,
                start_line=s.location.line,
                end_line=s.location.end_line,
                layer=layer,
                metrics=NodeMetrics(
                    loc=(s.location.end_line or s.location.line) - s.location.line + 1,
                    complexity=s.complexity,
                ),
                position=Position(x=0, y=0),
                symbol=s.qualname,
            ),
            column,
        )

    # A __main__ block is an entry point of its own: top-level code under the guard runs there.
    for m in repo.modules:
        if m.main_guard_line and not m.is_test:
            drafts[main_node_id(m.name)] = _Draft(
                GraphNode(
                    id=main_node_id(m.name),
                    label=f"{m.name} __main__",
                    type=NodeType.ENTRY,
                    file=m.path,
                    start_line=m.main_guard_line,
                    end_line=m.loc,
                    layer=Layer.ENTRY,
                    metrics=NodeMetrics(loc=m.loc - m.main_guard_line + 1),
                    position=Position(x=0, y=0),
                    symbol=m.name,
                ),
                COLUMN_INDEX["entry"],
            )

    def resource(key: str) -> str:
        node_id = resource_node_id(key)
        if node_id not in drafts:
            tech = KNOWN_TECH[key]
            is_db = tech.category in DATA_CATEGORIES
            drafts[node_id] = _Draft(
                GraphNode(
                    id=node_id,
                    label=tech.name,
                    type=NodeType.DATABASE if is_db else NodeType.EXTERNAL,
                    layer=Layer.DATA if is_db else None,
                    position=Position(x=0, y=0),
                    symbol=key,
                ),
                COLUMN_INDEX["database" if is_db else "external"],
            )
        return node_id

    def source_of(caller: str, line: int) -> str | None:
        """The node for code making a call: its function, or the __main__ block it sits in."""
        if function_node_id(caller) in drafts:
            return function_node_id(caller)
        module = modules.get(caller)
        if module and module.main_guard_line and line >= module.main_guard_line and main_node_id(caller) in drafts:
            return main_node_id(caller)
        return None  # import-time code, or a caller that is not graphed (e.g. a test)

    def internal_target(qualname: str) -> str | None:
        symbol = symbols.get(qualname)
        if symbol and symbol.kind is SymbolKind.CLASS:
            qualname = f"{qualname}.__init__"  # dataclasses have none, so they drop out
        node_id = function_node_id(qualname)
        return node_id if node_id in drafts else None

    edges: dict[tuple[str, str], EdgeKind] = {}
    call_sites: Counter[str] = Counter()

    def connect(source: str, target: str, kind: EdgeKind) -> None:
        current = edges.get((source, target))
        if source != target and (current is None or EDGE_PRIORITY[kind] > EDGE_PRIORITY[current]):
            edges[(source, target)] = kind

    for call in repo.calls:
        source = source_of(call.caller, call.location.line)
        if source is None or call.target is None:
            continue
        if call.resolution is CallResolution.INTERNAL:
            if target := internal_target(call.target):
                connect(source, target, EdgeKind.CALL)
                call_sites[target] += 1
        elif call.resolution is CallResolution.EXTERNAL and (key := tech_key(call.target)) in graphed_tech:
            target = resource(key)
            connect(source, target, EdgeKind.DATA if KNOWN_TECH[key].category in DATA_CATEGORIES else EdgeKind.CALL)
            call_sites[target] += 1

    for s in repo.symbols:
        source = function_node_id(s.qualname)
        if source not in drafts:
            continue
        for ref in s.references:
            if ref in symbols:
                # A referenced class is an annotation or isinstance check, not a construction: no edge.
                if symbols[ref].kind is not SymbolKind.CLASS and (target := internal_target(ref)):
                    connect(source, target, EdgeKind.IMPORT)
            elif (key := tech_key(ref)) in graphed_tech:
                connect(source, resource(key), EdgeKind.IMPORT)

    graph_edges = [
        GraphEdge(
            id=f"{kind.value}:{source}->{target}",
            source=source,
            target=target,
            kind=kind,
            animated=kind is EdgeKind.DATA,
        )
        for (source, target), kind in sorted(edges.items())
    ]

    fan_in: dict[str, set[str]] = defaultdict(set)
    fan_out: dict[str, set[str]] = defaultdict(set)
    for e in graph_edges:
        fan_in[e.target].add(e.source)
        fan_out[e.source].add(e.target)
    for node_id, draft in drafts.items():
        metrics = draft.node.metrics.model_copy(
            update={"fan_in": len(fan_in[node_id]), "fan_out": len(fan_out[node_id]), "call_sites": call_sites[node_id]}
        )
        draft.node = draft.node.model_copy(update={"metrics": metrics})

    nodes, columns = layout(list(drafts.values()), graph_edges)
    return Graph(nodes=nodes, edges=graph_edges, columns=columns)


def _function_type(s: Symbol, layer: Layer | None, entry_symbols: set[str]) -> NodeType:
    if s.qualname in entry_symbols or layer is Layer.ENTRY:
        return NodeType.ENTRY
    if layer is Layer.SERVICE:
        return NodeType.SERVICE
    return NodeType.FUNCTION


def _label(s: Symbol) -> str:
    """Qualname without the module: "OrderService.place_order", "make_wsgi_app.app"."""
    return s.qualname.removeprefix(s.module + ".")


# --- Layout -------------------------------------------------------------------


def layout(drafts: list[_Draft], edges: list[GraphEdge]) -> tuple[list[GraphNode], list[GraphColumn]]:
    """Columns by layer; rows ordered to keep edges short (barycenter sweeps); each column centered."""
    by_column: dict[int, list[_Draft]] = defaultdict(list)
    for d in drafts:
        by_column[d.column].append(d)
    used = sorted(by_column)  # empty columns are dropped, so there are no gaps
    order = {c: sorted(by_column[c], key=_reading_order) for c in used}

    column_of = {d.node.id: d.column for d in drafts}
    neighbours: dict[str, list[str]] = defaultdict(list)
    for e in edges:
        if column_of[e.source] != column_of[e.target]:
            neighbours[e.source].append(e.target)
            neighbours[e.target].append(e.source)

    def y_positions() -> dict[str, float]:
        ys = {}
        for c in used:
            middle = (len(order[c]) - 1) / 2
            for row, d in enumerate(order[c]):
                ys[d.node.id] = (row - middle) * ROW_HEIGHT
        return ys

    for sweep in range(LAYOUT_SWEEPS):
        sweep_columns = used[1:] if sweep % 2 == 0 else list(reversed(used[:-1]))
        for c in sweep_columns:
            ys = y_positions()

            def pull(d: _Draft) -> float:
                linked = [ys[n] for n in neighbours[d.node.id]]
                return sum(linked) / len(linked) if linked else ys[d.node.id]

            order[c] = sorted(order[c], key=pull)  # stable: equal pull keeps reading order

    ys = y_positions()
    top = min(ys.values(), default=0.0)
    nodes: list[GraphNode] = []
    columns: list[GraphColumn] = []
    for x_index, c in enumerate(used):
        key, title = COLUMNS[c]
        x = x_index * COLUMN_WIDTH
        columns.append(GraphColumn(key=key, title=title, x=x))
        nodes.extend(d.node.model_copy(update={"position": Position(x=x, y=ys[d.node.id] - top)}) for d in order[c])
    return nodes, columns


def _reading_order(d: _Draft) -> tuple[str, int, str]:
    return (d.node.file or "~", d.node.start_line or 0, d.node.label)

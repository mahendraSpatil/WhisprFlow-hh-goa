"""Lookups over the PipelineArchitect graph shared by the diagnosis agents."""

from __future__ import annotations

from collections import defaultdict, deque

from app.models.agents import EdgeKind, Graph, GraphNode, NodeType

CODE_TYPES = {NodeType.ENTRY, NodeType.FUNCTION, NodeType.SERVICE}


class NodeIndex:
    def __init__(self, graph: Graph | None) -> None:
        self.graph = graph
        self.nodes: dict[str, GraphNode] = {}
        self._by_file: dict[str, list[GraphNode]] = defaultdict(list)
        self._out: dict[str, list[str]] = defaultdict(list)
        if graph is None:
            return
        for node in graph.nodes:
            self.nodes[node.id] = node
            if node.type in CODE_TYPES and node.file and node.start_line is not None:
                self._by_file[node.file].append(node)
        for edge in graph.edges:
            if edge.kind in (EdgeKind.CALL, EdgeKind.IMPORT):  # data edges lead into databases, not through them
                self._out[edge.source].append(edge.target)
        for targets in self._out.values():
            targets.sort()

    def at(self, file: str, line: int) -> GraphNode | None:
        """The innermost code node whose span contains the line."""
        best: GraphNode | None = None
        for node in self._by_file.get(file, ()):
            end = node.end_line if node.end_line is not None else node.start_line
            if node.start_line <= line <= end and (
                best is None or (end - node.start_line) < ((best.end_line or best.start_line) - best.start_line)
            ):
                best = node
        return best

    def by_symbol(self, qualname: str) -> GraphNode | None:
        return self.nodes.get(f"fn:{qualname}")

    def entry_path(self, target_id: str) -> list[GraphNode] | None:
        """Shortest call path from any entry node to the target (inclusive), or None."""
        if target_id not in self.nodes:
            return None
        entries = sorted(n.id for n in self.nodes.values() if n.type is NodeType.ENTRY)
        parent: dict[str, str | None] = {e: None for e in entries}
        queue = deque(entries)
        while queue:
            current = queue.popleft()
            if current == target_id:
                break
            for nxt in self._out.get(current, ()):
                if nxt not in parent and self.nodes[nxt].type in CODE_TYPES:
                    parent[nxt] = current
                    queue.append(nxt)
        if target_id not in parent:
            return None
        path: list[GraphNode] = []
        cursor: str | None = target_id
        while cursor is not None:
            path.append(self.nodes[cursor])
            cursor = parent[cursor]
        return list(reversed(path))

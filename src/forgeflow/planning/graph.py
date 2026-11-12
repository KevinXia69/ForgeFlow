"""Small tenant-scoped business relationship graph used by planning."""
from __future__ import annotations

from dataclasses import dataclass, field


class GraphError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Node:
    kind: str
    id: str


@dataclass(slots=True)
class BusinessGraph:
    nodes: set[Node] = field(default_factory=set)
    edges: dict[Node, list[tuple[Node, str]]] = field(default_factory=dict)

    def add_node(self, kind: str, node_id: str) -> Node:
        if not kind or not node_id:
            raise GraphError("graph_node_invalid")
        node = Node(kind, node_id)
        self.nodes.add(node)
        self.edges.setdefault(node, [])
        return node

    def link(self, source: Node, target: Node, relation: str) -> None:
        if source not in self.nodes or target not in self.nodes:
            raise GraphError("graph_node_missing")
        if not relation:
            raise GraphError("graph_relation_invalid")
        if source == target:
            raise GraphError("graph_self_loop")
        edge = (target, relation)
        if edge not in self.edges[source]:
            self.edges[source].append(edge)

    def neighbours(self, node: Node, relation: str | None = None) -> list[Node]:
        return [target for target, kind in self.edges.get(node, [])
                if relation is None or kind == relation]

    def path(self, source: Node, target: Node) -> list[Node] | None:
        if source not in self.nodes or target not in self.nodes:
            raise GraphError("graph_node_missing")
        queue = [(source, [source])]
        seen = {source}
        while queue:
            current, route = queue.pop(0)
            if current == target:
                return route
            for neighbour in self.neighbours(current):
                if neighbour not in seen:
                    seen.add(neighbour)
                    queue.append((neighbour, [*route, neighbour]))
        return None

    def cycles(self) -> list[Node]:
        visited: set[Node] = set()
        active: set[Node] = set()
        found = []

        def visit(node: Node) -> None:
            visited.add(node)
            active.add(node)
            for neighbour in self.neighbours(node):
                if neighbour in active:
                    found.append(neighbour)
                elif neighbour not in visited:
                    visit(neighbour)
            active.remove(node)

        for node in self.nodes:
            if node not in visited:
                visit(node)
        return found

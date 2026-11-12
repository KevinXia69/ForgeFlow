from __future__ import annotations

import pytest

from forgeflow.planning.graph import BusinessGraph, GraphError


def test_relationship_path_and_cycle_detection() -> None:
    graph = BusinessGraph()
    customer = graph.add_node("customer", "C1")
    sales = graph.add_node("sales_order", "SO1")
    product = graph.add_node("product", "P1")
    component = graph.add_node("product", "C1")
    graph.link(customer, sales, "orders")
    graph.link(sales, product, "contains")
    graph.link(product, component, "bom")
    assert graph.path(customer, component) == [customer, sales, product, component]
    graph.link(component, product, "bom")
    assert graph.cycles()


def test_invalid_graph_edges_fail_closed() -> None:
    graph = BusinessGraph()
    node = graph.add_node("product", "P1")
    with pytest.raises(GraphError, match="self_loop"):
        graph.link(node, node, "bom")
    with pytest.raises(GraphError, match="relation_invalid"):
        graph.link(node, graph.add_node("product", "P2"), "")


"""Scope totals are graph-derived and remain separate from plan demand and delivery."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forgeflow.api.case_qa import _get
from forgeflow.api.impact_context import (
    _direct_scope,
    checked_chain,
    impact_scope_text,
    impact_summary,
)

ROOT = Path(__file__).resolve().parents[1]


def _task_graph(task):
    """Build a trusted minimal graph from pinned benchmark relations for aggregate tests."""
    data = json.loads((ROOT / 'vendor/erp-bench/tasks' / task / 'environment/scenario_data.json').read_text())
    repair = data['repair_context']
    broken = set(repair['broken_purchase_order_refs'] + repair['broken_manufacturing_order_refs'])
    nodes = [{'key': 'disruption', 'kind': 'disruption', 'product_code': None}]
    edges, orders = [], []
    by_customer = {}
    for row in data['existing_sales_orders']:
        node = {'key': row['ref'], 'kind': 'sales_order', 'product_code': row['product_code'], 'quantity': row['quantity']}
        nodes.append(node)
        by_customer[row['customer_ref']] = row['ref']
        orders.append({'node_key': row['ref'], 'order_ref': row['ref'], 'readiness': {'demand_units': row['quantity']}})
    for row in data['existing_purchase_orders'] + data['existing_manufacturing_orders']:
        if row['ref'] not in broken:
            continue
        nodes.append({'key': row['ref'], 'kind': 'purchase_order' if row['ref'] in repair['broken_purchase_order_refs'] else 'manufacturing_order',
                      'product_code': row['product_code'], 'quantity': row['quantity']})
        edges.append({'from': 'disruption', 'to': row['ref'], 'phase': 'baseline', 'relation': 'disruption_scope'})
        for customer in row['origin_customer_refs']:
            edges.append({'from': row['ref'], 'to': by_customer[customer], 'phase': 'baseline', 'relation': 'origin_scope'})
    return {'nodes': nodes, 'edges': edges, 'orders': orders}


@pytest.mark.parametrize(('task', 'all_orders', 'all_units', 'linked_orders', 'linked_units', 'broken_docs', 'broken_units'), [
    ('2280_medium_repair_plan_medium', 5, 50, 1, 13, 1, 3),
    ('2299_hard_repair_plan_hard', 12, 228, 6, 112, 3, 92),
])
def test_pinned_a_and_b_scope_comes_from_relations(task, all_orders, all_units, linked_orders, linked_units, broken_docs, broken_units):
    graph = _task_graph(task)
    before = copy.deepcopy(graph)
    summary, orders, supplies, edges = _direct_scope(graph)
    assert len(graph['orders']) == all_orders
    assert sum(row['readiness']['demand_units'] for row in graph['orders']) == all_units
    assert summary['direct_order_count'] == linked_orders
    assert summary['linked_demand_by_product'][0]['units'] == linked_units
    assert summary['failed_supply_by_product'][0]['count'] == broken_docs
    assert summary['failed_supply_by_product'][0]['units'] == broken_units
    assert len(orders) == linked_orders and len(supplies) == broken_docs and edges
    assert graph == before


def test_shared_path_counts_orders_and_failed_nodes_once_without_mixing_skus():
    graph = _task_graph('2280_medium_repair_plan_medium')
    broken = next(node for node in graph['nodes'] if node['kind'] == 'purchase_order')
    target = next(edge['to'] for edge in graph['edges'] if edge['from'] == broken['key'])
    graph['nodes'].append({'key': 'raw', 'kind': 'purchase_order', 'product_code': 'RAW', 'quantity': 8})
    graph['edges'] += [
        {'from': 'disruption', 'to': 'raw', 'phase': 'baseline', 'relation': 'disruption_scope'},
        {'from': 'raw', 'to': broken['key'], 'phase': 'baseline', 'relation': 'origin_scope'},
        {'from': 'raw', 'to': target, 'phase': 'baseline', 'relation': 'origin_scope'},
        {'from': broken['key'], 'to': 'raw', 'phase': 'baseline', 'relation': 'origin_scope'},
        {'from': 'raw', 'to': graph['orders'][0]['node_key'], 'phase': 'planned', 'relation': 'sales_allocation'},
    ]
    summary, *_ = _direct_scope(graph)
    assert summary['direct_order_count'] == 1
    assert summary['linked_demand_by_product'][0]['units'] == 13
    assert {row['product_code']: row['units'] for row in summary['failed_supply_by_product']} == {broken['product_code']: 3, 'RAW': 8}


def test_checked_projection_exposes_scalar_sources_without_promoting_missing_graph():
    report = json.loads((ROOT / 'web/e2e/fixtures/impact-a-reprojection.json').read_text())
    plan = report['cases'][0]['replay_case']['plan']
    before = copy.deepcopy(plan)
    assert checked_chain(plan) is not None
    assert impact_summary(plan)['direct_scope']['direct_order_count'] == 1
    for lang in ('zh', 'en'):
        text, specs = impact_scope_text(plan, lang)
        assert '13' in text and '3' in text
        assert '50' not in text
        for path, _, _ in specs:
            assert not isinstance(_get({'plan': plan}, path), (dict, list))
        assert any(path.endswith('readiness.demand_units') for path, _, _ in specs)
        assert any(path.endswith('.quantity') for path, _, _ in specs)
    assert plan == before
    for missing in (None, {}, {**plan, 'impact_chain': None}, {**plan, 'plan_id': 'f' * 64}):
        text, specs = impact_scope_text(missing, 'en')
        assert 'not recorded' in text
        assert not any('impact_chain' in path for path, _, _ in specs)

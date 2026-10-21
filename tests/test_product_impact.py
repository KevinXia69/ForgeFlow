"""Invented ERP records; impact relationships cannot invent delivery or allocation."""
from __future__ import annotations

import copy
import importlib.util
import json

import pytest
from test_recovery_change_set import bind, fixture

from forgeflow.product.impact import build_impact_chain, project_impact_chain
from forgeflow.product.readiness import build_order_readiness
from forgeflow.recovery.change_set import build_change_set
from forgeflow.recovery.storage import digest

pytestmark = pytest.mark.skipif(importlib.util.find_spec("ortools") is None,
                               reason="Invented change-set fixtures require optional planning dependencies.")


def built(inputs=None):
    inputs = fixture() if inputs is None else inputs
    request, snapshot, blueprint, plan = inputs
    changes = build_change_set(*inputs)
    keys = {"plan_id": "a" * 64, "snapshot_sha256": digest(snapshot)}
    readiness = build_order_readiness(request, blueprint, plan["plan"], changes, **keys)
    value = build_impact_chain(*inputs, changes, readiness, **keys)
    return value, inputs, changes, readiness, keys


def supplier_inputs():
    request, snapshot, _, _ = fixture()
    request["disruption"] = {"kind": "supplier_cancellation", "vendor_ref": "V1"}
    partner = copy.deepcopy(snapshot["models"]["res.partner"][1])
    partner.update(id=3, ref="V2", name="Alternative supplier")
    snapshot["models"]["res.partner"].append(partner)
    offer = copy.deepcopy(snapshot["models"]["product.supplierinfo"][0])
    offer.update(id=2, partner_id=[3, "Alternative supplier"])
    snapshot["models"]["product.supplierinfo"].append(offer)
    inputs = bind(request, snapshot)
    inputs[3]["plan"]["purchase_orders"][0].update(vendor_ref="V2", plan_ref="p:RAW:V2", offer_key="V2|RAW|1|2.0|1")
    return inputs


def two_order_inputs(*, parallel=False):
    request, snapshot, _, _ = fixture()
    request["sales_order_ids"] = [1, 2]
    snapshot["scope"]["sales_order_ids"] = [1, 2]
    request["sales_order_budget_caps"]["2"] = 2000
    customer = copy.deepcopy(snapshot["models"]["res.partner"][0])
    customer.update(id=3, ref="C2", name="Customer 2")
    snapshot["models"]["res.partner"].append(customer)
    sale = copy.deepcopy(snapshot["models"]["sale.order"][0])
    sale.update(id=2, name="SO2", partner_id=[3, "Customer 2"])
    snapshot["models"]["sale.order"].append(sale)
    line = copy.deepcopy(snapshot["models"]["sale.order.line"][0])
    snapshot["models"]["sale.order.line"][0]["product_uom_qty"] = 6
    line.update(id=2, order_id=[2, "SO2"], product_uom_qty=4)
    snapshot["models"]["sale.order.line"].append(line)
    for mo in snapshot["models"]["mrp.production"]:
        mo["origin"] = "SO1, SO2"
        if mo["id"] == 2:
            mo["product_qty"] = 10
    snapshot["models"]["purchase.order.line"][0]["product_qty"] = 20
    inputs = bind(request, snapshot)
    plan = inputs[3]["plan"]
    allocation = plan["allocations"][0]
    plan["allocations"] = [{**allocation, "ref": f"C{i}", "task_order_ref": f"SO{i}",
                            "demand": quantity, "fulfilled_demand": quantity, "assembly_units": quantity}
                           for i, quantity in ((1, 6), (2, 4))]
    plan["manufacturing_orders"][0]["origin_customer_refs"] = ["C1", "C2"]
    if parallel:
        bucket = plan["manufacturing_orders"][0]
        plan["manufacturing_orders"] = [{**bucket, "quantity": 5, "plan_ref": bucket["plan_ref"] + suffix}
                                         for suffix in (".a", ".b")]
        plan["purchase_orders"][0]["origin_plan_refs"] = [row["plan_ref"] for row in plan["manufacturing_orders"]]
    return inputs


def test_workcenter_chain_keeps_before_after_and_safe_clickable_source_facts():
    inputs = fixture()
    original = copy.deepcopy(inputs)
    value, _, changes, readiness, keys = built(inputs)
    assert inputs == original
    assert {node["action"] for node in value["nodes"]} >= {"keep", "update", "cancel", "create"}
    nodes = {node["key"]: node for node in value["nodes"]}
    assert nodes["baseline:existing:purchase.order:1"]["quantity"] == 12
    assert nodes["planned:existing:purchase.order:1"]["quantity"] == 20
    assert nodes["baseline:existing:mrp.production:1"]["action"] == "cancel"
    assert "planned:existing:mrp.production:1" not in nodes
    for node in value["nodes"]:
        assert node["facts"] and node["source_refs"]
    for entry in value["nodes"] + value["edges"]:
        for ref in entry["source_refs"]:
            record = value["source_facts"]
            for key in ref["record"].split("."):
                record = record[int(key)] if isinstance(record, list) else record[key]
            assert all(field in record for field in ref["fields"])
    assert value["orders"][0]["readiness"] == readiness["orders"][0]
    assert value["basis"]["source_sha256"]["change_set"] == digest(changes)
    assert project_impact_chain(value, readiness=readiness, **keys) == value
    text = json.dumps(value)
    assert "preconditions" not in text and "source_records" not in text and "product.supplierinfo" not in text
    assert "actual_delivery_unmeasured" in value["missing"]


def test_component_supplier_scope_keeps_multi_origin_quantities_unknown():
    value, _, _, _, _ = built(supplier_inputs())
    baseline = [row for row in value["edges"] if row["phase"] == "baseline"]
    assert baseline and all(row["quantity"] is None for row in baseline)
    po_edges = [row for row in baseline if row["from"] == "baseline:existing:purchase.order:1"]
    assert len(po_edges) == 2 and {row["product_code"] for row in po_edges} == {"RAW"}
    summary = value["orders"][0]
    assert "baseline:existing:purchase.order:1" in summary["baseline_node_keys"]
    assert "baseline:existing:mrp.production:1" in summary["baseline_node_keys"]
    assert "baseline_allocation_quantity_unrecorded" in summary["missing"]


def test_shared_manufacturing_components_are_not_allocated_again_to_each_sales_order():
    value, _, _, _, _ = built(two_order_inputs())
    component = [row for row in value["edges"] if row["relation"] == "component_allocation"]
    assert sum(row["quantity"] for row in component) == 20
    assert {row["product_code"] for row in component} == {"RAW"}
    assert [row["readiness"]["covered_units"] for row in value["orders"]] == [6, 4]
    for summary in value["orders"]:
        assert all(row["key"] in summary["planned_edge_keys"] for row in component)
        assert "component_order_allocation_unrecorded" in summary["missing"]
    assert sum(row["quantity"] for row in value["edges"] if row["relation"] == "sales_allocation") == 10


def test_parallel_sales_flows_keep_distinct_source_records_instead_of_being_merged_or_rejected():
    value, _, changes, _, _ = built(two_order_inputs(parallel=True))
    flows = changes["projected_state"]["sales_order_flows"]
    assert len(flows) > len({(row["source_token"], row["sales_order"]["token"]) for row in flows})
    edges = [row for row in value["edges"] if row["relation"] == "sales_allocation"]
    assert len(edges) == len(flows) and len({row["key"] for row in edges}) == len(flows)
    assert [row["quantity"] for row in edges] == [row["quantity"] for row in flows]


def test_moq_surplus_is_a_separate_unallocated_node_not_sales_or_component_consumption():
    request, snapshot, _, _ = fixture()
    snapshot["models"]["product.supplierinfo"][0]["min_qty"] = 21
    inputs = bind(request, snapshot)
    inputs[3]["plan"]["purchase_orders"][0].update(offer_key="V1|RAW|21|2.0|1", quantity=21)
    inputs[3]["plan"]["expected_new_spend"] = 72
    value, _, _, _, _ = built(inputs)
    unused = [node for node in value["nodes"] if node["kind"] == "unallocated_supply"]
    assert len(unused) == 1 and unused[0]["quantity"] == 1 and unused[0]["product_code"] == "RAW"
    assert unused[0]["key"] not in value["orders"][0]["planned_node_keys"]
    assert sum(row["quantity"] for row in value["edges"] if row["relation"] == "component_allocation") == 20


def test_stock_uses_its_own_sku_and_actual_planned_usage_without_turning_remaining_stock_into_flow():
    request, snapshot, _, _ = fixture()
    snapshot["models"]["stock.quant"][0]["quantity"] = 2
    snapshot["models"]["stock.quant"][1]["quantity"] = 5
    inputs = bind(request, snapshot)
    plan = inputs[3]["plan"]
    plan["manufacturing_orders"][0]["quantity"] = 8
    plan["purchase_orders"][0]["quantity"] = 12
    plan["allocations"][0].update(stock_allocated=2, assembly_units=8)
    plan["stock_report"] = [{"code": "FIN", "used": 2}, {"code": "RAW", "used": 4}]
    plan["expected_new_spend"] = 48
    value, _, _, _, _ = built(inputs)
    sources = {row["product_code"]: row for row in value["source_facts"]["stock"]}
    assert sources["RAW"]["used_units"] == 4 and sources["RAW"]["remaining_units"] == 1
    edges = [row for row in value["edges"] if row["from"].startswith("planned:stock:")]
    assert {(row["product_code"], row["quantity"], row["relation"]) for row in edges} == {
        ("FIN", 2, "sales_allocation"), ("RAW", 4, "component_allocation")}
    assert value["orders"][0]["readiness"]["stock_units"] == 2


def add_finished_supplier(snapshot):
    partner = copy.deepcopy(snapshot["models"]["res.partner"][1])
    partner.update(id=3, ref="V-FIN", name="Finished supplier", comment="Maximum order quantity for FIN: 100 units")
    snapshot["models"]["res.partner"].append(partner)
    offer = copy.deepcopy(snapshot["models"]["product.supplierinfo"][0])
    offer.update(id=2, partner_id=[3, "Finished supplier"], product_tmpl_id=[1, "FIN"], price=10)
    snapshot["models"]["product.supplierinfo"].append(offer)


def test_finished_purchase_flow_goes_to_sales_order_without_becoming_component_or_delivery():
    request, snapshot, _, _ = fixture()
    add_finished_supplier(snapshot)
    inputs = bind(request, snapshot)
    plan = inputs[3]["plan"]
    plan["manufacturing_orders"][0]["quantity"] = 7
    plan["purchase_orders"][0]["quantity"] = 14
    plan["purchase_orders"].append({"plan_ref": "p:FIN:V-FIN", "offer_key": "V-FIN|FIN|1|10.0|1",
        "vendor_ref": "V-FIN", "product_code": "FIN", "quantity": 3, "unit_cost": 10,
        "lead_time": 1, "planned_arrival_days": 2, "supply_role": "finished",
        "origin_customer_refs": ["C1"], "origin_plan_refs": []})
    plan["allocations"][0].update(assembly_units=7, purchase_units=3)
    plan["expected_new_spend"] = 79
    value, _, _, _, _ = built(inputs)
    po = next(node for node in value["nodes"] if node["phase"] == "planned" and node["kind"] == "purchase_order" and node["product_code"] == "FIN")
    edges = [row for row in value["edges"] if row["from"] == po["key"]]
    assert len(edges) == 1 and edges[0]["relation"] == "sales_allocation" and edges[0]["quantity"] == 3
    assert value["orders"][0]["readiness"]["purchased_units"] == 3
    assert po["facts"]["record_id"] is None and po["facts"]["document_ref"] is None


def test_finished_supplier_cancellation_preserves_original_po_sales_scope():
    request, snapshot, _, _ = fixture()
    add_finished_supplier(snapshot)
    request["disruption"] = {"kind": "supplier_cancellation", "vendor_ref": "V-FIN"}
    po = copy.deepcopy(snapshot["models"]["purchase.order"][0])
    po.update(id=2, name="PO2", partner_id=[3, "Finished supplier"], origin="SO1")
    snapshot["models"]["purchase.order"].append(po)
    line = copy.deepcopy(snapshot["models"]["purchase.order.line"][0])
    line.update(id=2, order_id=[2, "PO2"], product_id=[1, "FIN"], product_qty=3, price_unit=10)
    snapshot["models"]["purchase.order.line"].append(line)
    value, _, _, _, _ = built(bind(request, snapshot))
    summary = value["orders"][0]
    assert summary["baseline_node_keys"] == ["baseline:existing:purchase.order:2", "disruption", "order:existing:sale.order:1"]
    assert all(row["quantity"] is None for row in value["edges"] if row["key"] in summary["baseline_edge_keys"])


def test_original_component_po_can_reference_sales_order_as_scope_without_claiming_component_delivery():
    inputs = supplier_inputs()
    inputs[1]["models"]["purchase.order"][0]["origin"] = "SO1"
    revised = bind(inputs[0], inputs[1])
    revised[3]["plan"] = inputs[3]["plan"]
    value, _, _, _, _ = built(revised)
    edge = next(row for row in value["edges"] if row["from"] == "baseline:existing:purchase.order:1")
    assert edge["to"] == "order:existing:sale.order:1"
    assert edge["product_code"] == "RAW" and edge["quantity"] is None and edge["relation"] == "origin_scope"


def test_missing_baseline_origin_does_not_join_all_case_orders_and_old_dates_can_be_negative():
    inputs = two_order_inputs()
    inputs[1]["models"]["mrp.production"][0].update(origin="SO1", date_start="2025-12-29 00:00:00", date_deadline="2025-12-30 00:00:00")
    # Rebuild after the input change, preserving the explicit split allocation.
    revised = bind(inputs[0], inputs[1])
    revised[3]["plan"] = inputs[3]["plan"]
    value, _, _, _, _ = built(revised)
    order = value["orders"][1]
    assert order["baseline_node_keys"] == order["baseline_edge_keys"] == []
    assert "baseline_relationship_unrecorded" in order["missing"]
    baseline = next(row for row in value["nodes"] if row["key"] == "baseline:existing:mrp.production:1")
    assert baseline["facts"]["available_day"] == -2


@pytest.mark.parametrize("fault", ["plan", "snapshot", "edge_quantity", "baseline_quantity", "missing_edge",
    "duplicate_edge", "dangling", "node_extra", "source_extra", "source_path", "bool_quantity", "flow_duplicate",
    "flow_product", "flow_date", "flow_quantity", "origin_cycle", "broken", "stock", "readiness", "unknown_field"])
def test_public_projection_rejects_altered_graph_source_or_binding(fault):
    value, _, _, readiness, keys = built()
    if fault in {"plan", "snapshot"}:
        value["basis"]["plan_id" if fault == "plan" else "snapshot_sha256"] = "f" * 64
    elif fault == "edge_quantity":
        next(row for row in value["edges"] if row["phase"] == "planned")["quantity"] += 1
    elif fault == "baseline_quantity":
        next(row for row in value["edges"] if row["phase"] == "baseline")["quantity"] = 12
    elif fault == "missing_edge":
        value["edges"].pop()
    elif fault == "duplicate_edge":
        value["edges"].append(copy.deepcopy(value["edges"][0]))
    elif fault == "dangling":
        value["edges"][0]["to"] = "unknown"
    elif fault == "node_extra":
        value["nodes"][0]["facts"]["private_path"] = "CANARY_PRIVATE"
    elif fault == "source_extra":
        value["source_facts"]["operations"][0]["secret"] = "CANARY_PRIVATE"
    elif fault == "source_path":
        value["nodes"][0]["source_refs"][0]["record"] = "https://example.test/private"
    elif fault == "bool_quantity":
        value["source_facts"]["operations"][0]["before"]["quantity"] = True
    elif fault == "flow_duplicate":
        value["source_facts"]["sales_order_flows"].append(copy.deepcopy(value["source_facts"]["sales_order_flows"][0]))
    elif fault.startswith("flow_"):
        field, replacement = {"flow_product": ("product_code", "RAW"), "flow_date": ("available_day", 0), "flow_quantity": ("quantity", 999)}[fault]
        value["source_facts"]["sales_order_flows"][0][field] = replacement
    elif fault == "origin_cycle":
        op = next(row for row in value["source_facts"]["operations"] if row["model"] == "mrp.production")
        op["before"]["origin_tokens"] = [op["entity_ref"]]
    elif fault == "broken":
        value["source_facts"]["disruption"]["broken_tokens"] = ["existing:mrp.production:2"]
    elif fault == "stock":
        value["source_facts"]["stock"][0]["remaining_units"] = 1
    elif fault == "readiness":
        readiness = copy.deepcopy(readiness)
        readiness["orders"][0]["planned_ready_day"] += 1
    else:
        value["private_url"] = "CANARY_PRIVATE"
    with pytest.raises(ValueError, match="^invalid_impact_chain$"):
        project_impact_chain(value, readiness=readiness, **keys)


@pytest.mark.parametrize("fault", ["blueprint", "change_set", "snapshot", "readiness"])
def test_builder_independently_checks_original_bound_artifacts_before_public_projection(fault):
    _, inputs, changes, readiness, keys = built()
    inputs = copy.deepcopy(inputs)
    if fault == "blueprint":
        inputs[2]["repair_context"]["broken_manufacturing_order_refs"] = ["MO2"]
    elif fault == "change_set":
        changes["projected_state"]["sales_order_flows"][0]["quantity"] += 1
    elif fault == "snapshot":
        inputs[1]["models"]["sale.order"][0]["name"] = "OTHER"
    else:
        readiness["orders"][0]["covered_units"] = 0
    with pytest.raises(ValueError, match="^invalid_impact_chain$"):
        build_impact_chain(*inputs, changes, readiness, **keys)


def test_legacy_absence_stays_absent_and_never_requires_newer_readiness():
    assert project_impact_chain(None, plan_id=None, snapshot_sha256=None, readiness=None) is None


def test_business_entity_references_survive_the_existing_public_secret_gate():
    from forgeflow.api.evidence import public_evidence

    value, _, _, readiness, keys = built()
    public = public_evidence(value)
    assert public == value
    assert project_impact_chain(public, readiness=readiness, **keys) == value

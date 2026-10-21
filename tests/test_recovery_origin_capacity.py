"""MOQ surplus must not double-count consumers across declared PO origins."""
from __future__ import annotations

import copy

import pytest
from test_recovery_change_set import bind, fixture

from forgeflow.recovery.change_set import (
    ChangeSetError,
    _origin_capacity_feasible,
    build_change_set,
    validate_projection,
)


def origin_fixture():
    request, snapshot, _, _ = fixture(quantity=11)
    models = snapshot["models"]
    models["mrp.bom.line"][0]["product_qty"] = 1
    existing = models["mrp.production"]
    for index in (3, 4):
        extra = copy.deepcopy(existing[-1])
        extra.update(id=index, name=f"MO{index}")
        existing.append(extra)
        workorder = copy.deepcopy(models["mrp.workorder"][-1])
        workorder.update(id=index, name=f"WO{index}", production_id=[index, f"MO{index}"])
        models["mrp.workorder"].append(workorder)
    for mo, quantity in zip(existing, (1, 2, 5, 3), strict=True):
        mo["product_qty"] = quantity
    for wo in models["mrp.workorder"][1:]:
        wo["workcenter_id"] = [2, "WC-B"]
    models["purchase.order"][0]["origin"] = "MO2, MO3, MO4"
    models["purchase.order.line"][0]["product_qty"] = 8
    models["product.supplierinfo"][0]["min_qty"] = 8
    supplier = copy.deepcopy(models["res.partner"][1])
    supplier.update(id=3, ref="V2", name="Second supplier")
    models["res.partner"].append(supplier)
    offer = copy.deepcopy(models["product.supplierinfo"][0])
    offer.update(id=2, partner_id=[3, "Second supplier"], min_qty=4, price=3)
    models["product.supplierinfo"].append(offer)
    request, snapshot, blueprint, wrapped = bind(request, snapshot, 11)
    plan = wrapped["plan"]
    first = plan["purchase_orders"][0]
    first.update(quantity=8, offer_key="V1|RAW|8|2.0|1")
    second = copy.deepcopy(first)
    second.update(plan_ref="p:RAW:V2", quantity=4, vendor_ref="V2",
                  offer_key="V2|RAW|4|3.0|1", unit_cost=3)
    plan["purchase_orders"].append(second)
    plan["expected_new_spend"] = 8 * 2 + 4 * 3 + 11 * 3
    return request, snapshot, blueprint, wrapped


def test_solver_reassigns_real_flows_to_make_moq_origins_jointly_coverable():
    inputs = origin_fixture()
    before = copy.deepcopy(inputs)
    result = build_change_set(*inputs)
    state = result["projected_state"]
    pos, mos = state["purchase_orders"], state["manufacturing_orders"]
    assert inputs == before
    assert sorted(po["quantity"] for po in pos) == [4, 8]
    assert sorted(mo["quantity"] for mo in mos) == [1, 2, 3, 5]
    assert state["totals"]["unused_purchase_units"] == 1
    assert sum(row["quantity"] for row in state["component_flows"]) == 11
    assert _origin_capacity_feasible(pos, mos, {"RAW": 1})
    # Independently enumerate the small capacity network instead of asserting
    # a particular optimizer tie-break or fabricated origin strings.
    demand = {mo["target"]["token"]: mo["quantity"] for mo in mos}
    required = [min(po["quantity"], sum(demand[ref["token"]] for ref in po["origin_refs"])) for po in pos]
    assert sum(required) <= sum(demand.values())
    for po in pos:
        actual_consumers = {row["manufacturing_token"] for row in state["component_flows"]
                            if row["source_token"] == po["target"]["token"]}
        assert {ref["token"] for ref in po["origin_refs"]} == actual_consumers


def test_joint_origin_check_rejects_shared_capacity_and_accepts_real_reallocation():
    mos = [{"target": {"token": str(i)}, "quantity": quantity}
           for i, quantity in enumerate((2, 5, 3, 1))]

    def po(quantity, linked):
        return {"quantity": quantity, "product_code": "RAW", "supply_role": "component",
                "origin_refs": [{"token": str(i)} for i in linked]}

    # 8 + 4 declares twelve covered units against eleven units of real demand.
    assert not _origin_capacity_feasible([po(8, (0, 1, 2)), po(4, (2, 3))], mos, {"RAW": 1})
    # Same procurement, same demand and one legal unused unit; different actual
    # consumers let the second minimum order cover only the three-unit MO.
    assert _origin_capacity_feasible([po(8, (0, 1, 3)), po(4, (2,))], mos, {"RAW": 1})
    # Aggregate demand alone is insufficient when all origins point to one MO.
    assert not _origin_capacity_feasible([po(2, (0,)), po(2, (0,))], mos, {"RAW": 1})


def test_independent_validator_rejects_consistent_but_overlapping_origin_flows():
    inputs = origin_fixture()
    result = build_change_set(*inputs)
    state = result["projected_state"]
    old, new = sorted(state["purchase_orders"], key=lambda po: po["target"]["id"] is None)
    mos = {mo["quantity"]: mo for mo in state["manufacturing_orders"]}
    assignment = ((old, mos[2], 2), (old, mos[5], 5), (old, mos[3], 1),
                  (new, mos[3], 2), (new, mos[1], 1))
    plan_refs = {row["source_token"]: row["source_plan_ref"] for row in state["component_flows"]}
    state["component_flows"] = [{
        "source_token": po["target"]["token"], "manufacturing_token": mo["target"]["token"],
        "quantity": quantity, "product_code": "RAW", "available_day": po["arrival_day"],
        "required_day": mo["start_day"], "source_plan_ref": plan_refs[po["target"]["token"]],
    } for po, mo, quantity in assignment]
    state["unused_purchase_units"] = {new["target"]["token"]: 1}
    for po in (old, new):
        refs = sorted((mo["target"] for row_po, mo, _ in assignment if row_po is po), key=lambda ref: ref["token"])
        po["origin_refs"] = refs
        operation = next(op for op in result["operations"] if op["target"] == po["target"])
        operation["after"]["origin_refs"] = copy.deepcopy(refs)
        operation["depends_on"] = ["op:" + ref["token"] for ref in refs]
        if operation["before"] == operation["after"]:
            operation["action"] = "keep"
    with pytest.raises(ChangeSetError, match="origins overstate jointly coverable") as caught:
        validate_projection(result, *inputs)
    assert caught.value.code == "change_set_origin_capacity_mismatch"


def test_origin_capacity_keeps_separate_skus_and_allows_stock_headroom():
    mos = [{"target": {"token": "a"}, "quantity": 3}, {"target": {"token": "b"}, "quantity": 5}]
    def po(sku, quantity, *links):
        return {"product_code": sku, "supply_role": "component", "quantity": quantity,
                "origin_refs": [{"token": link} for link in links]}
    # RAW has two purchases with a split consumer; AUX leaves demand for stock.
    assert _origin_capacity_feasible([
        po("RAW", 3, "a", "b"), po("RAW", 5, "a", "b"), po("AUX", 7, "a", "b"),
    ], mos, {"RAW": 1, "AUX": 2})


@pytest.mark.parametrize("quantity", [0, -1, True, 0.5, float("nan"), float("inf")])
def test_origin_capacity_rejects_non_integral_business_units(quantity):
    mos = [{"target": {"token": "a"}, "quantity": 3}]
    pos = [{"product_code": "RAW", "supply_role": "component", "quantity": quantity,
            "origin_refs": [{"token": "a"}]}]
    assert not _origin_capacity_feasible(pos, mos, {"RAW": 1})

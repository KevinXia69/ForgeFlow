"""Real CP-SAT regressions for canceled leaf-component supplier offers."""

import copy
import hashlib
import json

import pytest
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.investigation.fixtures import FixtureConnection, supplier_cancellation_control
from forgeflow.planning.live import build_blueprint, collect_snapshot
from forgeflow.planning.tool import SolverFailure, solve_blueprint
from forgeflow.recovery.change_set import build_change_set, validate_projection

OBJECTIVES = [None, "preserve_quantities", "min_planned_cost", "preserve_capacity"]


def component_case():
    case = supplier_cancellation_control()
    records = case.records
    records["purchase.order"][0]["partner_id"] = 2
    records["purchase.order.line"][0].update(product_id=2, product_qty=20, price_unit=2)
    records["stock.move"][0].update(product_id=2, product_uom_qty=20)
    request = {
        "target_product_code": "FIN", "sales_order_ids": [1],
        "anchor_date": "2026-01-01", "margin": 0.2,
        "sales_order_budget_caps": {"1": 2000},
        "disruption": {"kind": "supplier_cancellation", "vendor_ref": "V-RAW",
                       "offer_key": "V-RAW|RAW|1|2.0|1"},
    }
    return records, request


def solve_records(records, request, bundle, objective=None):
    connection = FixtureConnection(records)
    before = connection.fingerprint()
    snapshot = collect_snapshot(connection, request)
    blueprint = build_blueprint(snapshot, request)
    wrapped = solve_blueprint(blueprint, bundle / "upstream", product_objective=objective)
    broken = blueprint["repair_context"]["broken_supplier_offer_key"]
    assert all(row["offer_key"] != broken for row in wrapped["plan"]["purchase_orders"])
    changes = build_change_set(request, snapshot, blueprint, wrapped,
                               product_objective=objective)
    validate_projection(changes, request, snapshot, blueprint, wrapped,
                        product_objective=objective)
    assert connection.fingerprint() == before
    assert connection.write_attempts == []
    return wrapped["plan"]


@pytest.mark.parametrize("objective", OBJECTIVES)
def test_component_cancellation_uses_replacement_component_for_every_objective(objective, real_bundle):
    records, request = component_case()
    # Eliminate finished supply: the solution must buy replacement RAW and build FIN.
    records["product.supplierinfo"] = records["product.supplierinfo"][:1]
    alternate = copy.deepcopy(records["product.supplierinfo"][0])
    alternate.update(id=4, partner_id=4, price=3)
    records["product.supplierinfo"].append(alternate)
    records["res.partner"][3]["comment"] = "Maximum order quantity for RAW: 100 units"
    plan = solve_records(records, request, real_bundle, objective)
    assert [(row["vendor_ref"], row["product_code"], row["quantity"])
            for row in plan["purchase_orders"]] == [("V-NEW", "RAW", 20)]
    assert sum(row["quantity"] for row in plan["manufacturing_orders"]) == 10


@pytest.mark.parametrize("objective", OBJECTIVES)
def test_component_cancellation_without_replacement_is_really_infeasible(objective, real_bundle):
    records, request = component_case()
    records["product.supplierinfo"] = records["product.supplierinfo"][:1]
    connection = FixtureConnection(records)
    before = connection.fingerprint()
    blueprint = build_blueprint(collect_snapshot(connection, request), request)
    with pytest.raises(SolverFailure) as failure:
        solve_blueprint(blueprint, real_bundle / "upstream", product_objective=objective)
    assert str(failure.value) == "planning_model_infeasible"
    assert failure.value.blueprint == blueprint
    assert failure.value.blueprint is not blueprint
    assert failure.value.blueprint_sha256 == hashlib.sha256(
        json.dumps(blueprint, sort_keys=True, allow_nan=False).encode()
    ).hexdigest()
    calls = failure.value.trace["solve_calls"]
    assert calls[-1]["status"] == "INFEASIBLE"
    assert connection.fingerprint() == before
    assert connection.write_attempts == []


def test_component_cancellation_does_not_disable_same_vendor_other_product(real_bundle):
    records, request = component_case()
    # The canceled offer is RAW only; FIN from that same vendor remains valid.
    records["product.supplierinfo"] = records["product.supplierinfo"][:2]
    records["product.supplierinfo"][1]["partner_id"] = 2
    plan = solve_records(records, request, real_bundle, "min_planned_cost")
    assert [(row["vendor_ref"], row["product_code"], row["quantity"])
            for row in plan["purchase_orders"]] == [("V-RAW", "FIN", 10)]
    assert not plan["manufacturing_orders"]


def test_component_cancellation_keeps_existing_component_stock_available(real_bundle):
    records, request = component_case()
    records["product.supplierinfo"] = records["product.supplierinfo"][:1]
    records["stock.quant"][1]["quantity"] = 20
    plan = solve_records(records, request, real_bundle)
    assert not plan["purchase_orders"]
    assert sum(row["quantity"] for row in plan["manufacturing_orders"]) == 10


def test_component_cancellation_can_fall_back_to_finished_purchase(real_bundle):
    records, request = component_case()
    plan = solve_records(records, request, real_bundle)
    assert [(row["vendor_ref"], row["product_code"], row["quantity"])
            for row in plan["purchase_orders"]] == [("V-OLD", "FIN", 10)]
    assert not plan["manufacturing_orders"]

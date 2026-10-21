"""Fail closed on business structures the pinned planner cannot represent."""

import copy
import runpy
from pathlib import Path

import pytest

from forgeflow.planning.live import LiveInputError, build_blueprint


@pytest.fixture
def business_fixture():
    fixture_module = runpy.run_path(str(Path(__file__).with_name("test_planning_live.py")))
    return fixture_module["fixture_data"]()


@pytest.mark.parametrize("state", ["draft", "confirmed", "progress"])
def test_other_active_manufacturing_in_same_company_is_rejected_without_shared_center(
    business_fixture, state,
):
    snapshot, request = business_fixture
    snapshot["models"]["mrp.production"].append({
        "id": 99,
        "name": "OUTSIDE-MO",
        "product_id": [99, "Outside product"],
        "company_id": [1, "Company"],
        "state": state,
    })
    # Its operation uses another center, so a shared-workcenter check alone
    # cannot establish whether it has reserved the selected graph's raw stock.
    snapshot["models"]["mrp.workorder"].append({
        "id": 99,
        "production_id": [99, "OUTSIDE-MO"],
        "workcenter_id": [99, "Other center"],
        "state": "ready",
    })
    snapshot["models"]["stock.quant"][-1]["reserved_quantity"] = 5
    before = copy.deepcopy(snapshot)
    with pytest.raises(LiveInputError, match="outside the BoM closure"):
        build_blueprint(snapshot, request)
    assert snapshot == before


@pytest.mark.parametrize(
    ("state", "company_id"), [("done", 1), ("cancel", 1), ("confirmed", 2)],
)
def test_inactive_or_other_company_outside_mo_does_not_block_supported_scope(
    business_fixture, state, company_id,
):
    snapshot, request = business_fixture
    snapshot["models"]["mrp.production"].append({
        "id": 99,
        "name": "OUTSIDE-MO",
        "product_id": [99, "Outside product"],
        "company_id": [company_id, "Company"],
        "state": state,
    })
    blueprint = build_blueprint(snapshot, request)
    assert {m["ref"] for m in blueprint["repair_context"]["seeded_manufacturing_orders"]} == {
        "MO-1", "MO-2",
    }


@pytest.mark.parametrize("changed_field", ["price", "min_qty", "delay"])
def test_multiple_component_offers_from_one_vendor_are_not_silently_collapsed(
    business_fixture, changed_field,
):
    snapshot, request = business_fixture
    offer = copy.deepcopy(snapshot["models"]["product.supplierinfo"][-1])
    offer["id"] = 99
    offer[changed_field] += 1
    snapshot["models"]["product.supplierinfo"].append(offer)
    with pytest.raises(LiveInputError, match="multiple offers from component vendor"):
        build_blueprint(snapshot, request)


def test_distinct_component_vendors_keep_independent_horizon_limits(business_fixture):
    snapshot, request = business_fixture
    vendor = copy.deepcopy(snapshot["models"]["res.partner"][-1])
    vendor.update(id=99, name="Other raw vendor", ref="V-RAW-OTHER")
    vendor["comment"] = "Maximum order quantity for RAW: 17 units"
    snapshot["models"]["res.partner"].append(vendor)
    offer = copy.deepcopy(snapshot["models"]["product.supplierinfo"][-1])
    offer.update(id=99, partner_id=[99, "Other raw vendor"], min_qty=3, price=2)
    snapshot["models"]["product.supplierinfo"].append(offer)
    blueprint = build_blueprint(snapshot, request)
    assert {
        (v["ref"], v["min_qty"], v["max_qty"])
        for v in blueprint["vendors"] if v["product_code"] == "RAW"
    } == {("V-RAW", 1, 200), ("V-RAW-OTHER", 3, 17)}


def test_manufactured_intermediate_purchase_offer_is_rejected_instead_of_ignored(
    business_fixture,
):
    snapshot, request = business_fixture
    offer = copy.deepcopy(snapshot["models"]["product.supplierinfo"][-1])
    offer.update(
        id=99,
        partner_id=[3, "Intermediate vendor"],
        product_tmpl_id=[2, "Intermediate template"],
        price=10,
    )
    snapshot["models"]["product.supplierinfo"].append(offer)
    with pytest.raises(LiveInputError, match="buying manufactured intermediate products"):
        build_blueprint(snapshot, request)

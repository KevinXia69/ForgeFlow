"""Independent synthetic records and fault injection; no local-run fixtures."""

import copy
import importlib.util
import json

import pytest

from forgeflow.planning.live import FIELDS, MARKER, build_blueprint
from forgeflow.recovery.change_set import (
    ChangeSetError,
    build_change_set,
    canonical_hash,
    validate_projection,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None,
    reason="Install optional planning dependencies to run recovery CP-SAT checks.",
)


def fixture(quantity=10):
    request = {
        "target_product_code": "FIN",
        "sales_order_ids": [1],
        "anchor_date": "2026-01-01",
        "margin": 0.2,
        "sales_order_budget_caps": {"1": 2000},
        "disruption": {"kind": "workcenter_outage", "workcenter_code": "WC-A"},
    }
    models = {model: [] for model in FIELDS}

    def add(model, **values):
        row = {field[0] if isinstance(field, tuple) else field: False for field in FIELDS[model]}
        row.update(values)
        models[model].append(row)

    for pid, sku, price in [(1, "FIN", 100), (2, "RAW", 2)]:
        add(
            "product.product",
            id=pid,
            name=sku,
            default_code=sku,
            product_tmpl_id=[pid, sku],
            list_price=price,
            standard_price=price / 2,
            type="consu",
            is_storable=True,
            route_ids=[],
            uom_id=[1, "Units"],
        )
    add("res.currency", id=1, name="USD")
    add("res.partner", id=1, name="Customer", ref="C1", email="", comment="", supplier_rank=0)
    add(
        "res.partner",
        id=2,
        name="Supplier",
        ref="V1",
        email="",
        comment="Maximum order quantity for RAW: 100 units",
        supplier_rank=1,
    )
    add(
        "sale.order",
        id=1,
        name="SO1",
        partner_id=[1, "Customer"],
        state="sale",
        commitment_date="2026-01-09 12:00:00",
        currency_id=[1, "USD"],
        company_id=[1, "Company"],
    )
    add(
        "sale.order.line",
        id=1,
        order_id=[1, "SO1"],
        product_id=[1, "FIN"],
        product_uom_qty=quantity,
        price_unit=100,
        qty_delivered=0,
        discount=0,
        product_uom_id=[1, "Units"],
    )
    add(
        "mrp.bom",
        id=1,
        product_id=False,
        product_tmpl_id=[1, "FIN"],
        product_qty=1,
        product_uom_id=[1, "Units"],
        type="normal",
        produce_delay=1,
        active=True,
    )
    add(
        "mrp.bom.line",
        id=1,
        bom_id=[1, "BoM"],
        product_id=[2, "RAW"],
        product_qty=2,
        product_uom_id=[1, "Units"],
    )
    add(
        "mrp.routing.workcenter",
        id=1,
        bom_id=[1, "BoM"],
        name="Assembly",
        workcenter_id=[1, "WC-A"],
        sequence=1,
    )
    for wid, wc, cap, unit in [(1, "WC-A", 0, 2), (2, "WC-B", 100, 3)]:
        note = (
            f"Benchmark capacity: {cap} total processing minutes across the scenario horizon.\n{MARKER}\n"
            + json.dumps(
                {
                    "product_code": "FIN",
                    "time_per_unit_minutes": unit,
                    "unit_cost": unit,
                    "lead_days": 1,
                }
            )
        )
        add(
            "mrp.workcenter",
            id=wid,
            name=wc,
            code=wc,
            note=note,
            alternative_workcenter_ids=[2] if wid == 1 else [],
            time_efficiency=100,
            time_start=0,
            time_stop=0,
        )
    add(
        "product.supplierinfo",
        id=1,
        partner_id=[2, "Supplier"],
        product_id=False,
        product_tmpl_id=[2, "RAW"],
        price=2,
        min_qty=1,
        delay=1,
        product_name="Standard",
        currency_id=[1, "USD"],
        company_id=[1, "Company"],
    )
    for mid, qty, wc in [(1, 4, 1), (2, 6, 2)]:
        add(
            "mrp.production",
            id=mid,
            name=f"MO{mid}",
            product_id=[1, "FIN"],
            product_qty=qty,
            product_uom_id=[1, "Units"],
            state="confirmed",
            origin="SO1",
            date_start="2026-01-04 12:00:00",
            date_deadline="2026-01-05 12:00:00",
            qty_produced=0,
            bom_id=[1, "BoM"],
            company_id=[1, "Company"],
        )
        add(
            "mrp.workorder",
            id=mid,
            name=f"WO{mid}",
            production_id=[mid, f"MO{mid}"],
            workcenter_id=[wc, f"WC-{wc}"],
            state="ready",
            product_id=[1, "FIN"],
        )
    add(
        "purchase.order",
        id=1,
        name="PO1",
        partner_id=[2, "Supplier"],
        state="purchase",
        origin="MO1, MO2",
        currency_id=[1, "USD"],
        company_id=[1, "Company"],
    )
    add(
        "purchase.order.line",
        id=1,
        order_id=[1, "PO1"],
        product_id=[2, "RAW"],
        product_qty=12,
        price_unit=2,
        qty_received=0,
        date_planned="2026-01-03 12:00:00",
        product_uom_id=[1, "Units"],
    )
    add("stock.location", id=1, usage="internal")
    for pid in (1, 2):
        add(
            "stock.quant",
            id=pid,
            product_id=[pid, "Product"],
            location_id=[1, "Stock"],
            quantity=0,
            reserved_quantity=0,
            company_id=[1, "Company"],
        )
    snapshot = {
        "schema_version": 1,
        "models": models,
        "scope": {
            "target_product_code": "FIN",
            "sales_order_ids": [1],
            "active_manufacturing_global": True,
        },
    }
    return bind(request, snapshot, quantity)


def bind(request, snapshot, quantity=10):
    blueprint = build_blueprint(snapshot, request)
    plan = {
        "objective_kind": "repair_plan",
        "expected_new_spend": quantity * 7,
        "manufacturing_orders": [
            {
                "plan_ref": "m:FIN:WC-B:5",
                "product_code": "FIN",
                "quantity": quantity,
                "workcenter_code": "WC-B",
                "unit_cost": 3,
                "lead_time": 1,
                "time_per_unit_minutes": 3,
                "level": 0,
                "needed_by_days": 5,
                "origin_customer_refs": ["C1"],
                "origin_plan_refs": [],
            }
        ],
        "purchase_orders": [
            {
                "plan_ref": "p:RAW:V1",
                "offer_key": "V1|RAW|1|2.0|1",
                "vendor_ref": "V1",
                "product_code": "RAW",
                "quantity": quantity * 2,
                "unit_cost": 2,
                "lead_time": 1,
                "planned_arrival_days": 2,
                "supply_role": "component",
                "origin_customer_refs": [],
                "origin_plan_refs": ["m:FIN:WC-B:5"],
            }
        ],
        "allocations": [
            {
                "ref": "C1",
                "accepted": True,
                "task_order_ref": "SO1",
                "demand": quantity,
                "fulfilled_demand": quantity,
                "deadline": 8,
                "stock_allocated": 0,
                "purchase_units": 0,
                "assembly_units": quantity,
            }
        ],
        "stock_report": [{"code": "FIN", "used": 0}, {"code": "RAW", "used": 0}],
    }
    wrapped = {
        "schema_version": 1,
        "odoo_writes": False,
        "blueprint_sha256": canonical_hash(blueprint),
        "plan": plan,
    }
    return request, snapshot, blueprint, wrapped


def test_synthetic_all_actions_and_ids():
    inputs = fixture()
    original = copy.deepcopy(inputs)
    result = build_change_set(*inputs)
    assert inputs == original
    ops = {(o["target"]["model"], o["target"]["id"]): o for o in result["operations"]}
    assert {o["action"] for o in result["operations"]} == {"keep", "update", "cancel", "create"}
    assert ops["mrp.production", 1]["action"] == "cancel"
    assert ops["mrp.production", 2]["action"] == "keep"
    assert ops["purchase.order", 1]["action"] == "update"
    assert ops["purchase.order", 1]["before"]["quantity"] == 12
    assert ops["purchase.order", 1]["after"]["quantity"] == 20
    assert ops["purchase.order", 1]["after"]["line_id"] == 1
    assert ops["purchase.order", 1]["after"]["line_name"] is None
    assert result["projected_state"]["totals"]["retained_manufactured_quantity"] == 6
    assert result["projected_state"]["totals"]["new_manufactured_quantity"] == 4
    assert len(result["plan_ref_map"]["m:FIN:WC-B:5"]) == 2
    assert not result["execution_ready"]
    assert result["required_preflight_reads"]
    assert all("duration_expected" in x["fields"] for x in result["required_preflight_reads"])
    assert all("method" not in o and "rpc" not in o for o in result["operations"])
    assert build_change_set(*inputs) == result


def test_existing_mo_can_resize_without_double_counting():
    result = build_change_set(*fixture(4))
    mo = next(
        o
        for o in result["operations"]
        if o["target"]["model"] == "mrp.production" and o["target"]["id"] == 2
    )
    assert (
        mo["action"] == "update" and mo["before"]["quantity"] == 6 and mo["after"]["quantity"] == 4
    )
    assert result["projected_state"]["totals"]["manufactured_quantity"] == 4
    assert result["projected_state"]["totals"]["new_manufactured_quantity"] == 0


@pytest.mark.parametrize(
    "start,due",
    [
        ("2026-01-06 00:00:00", "2026-01-07 00:00:00"),
        ("2026-01-01 00:00:00", "2026-01-02 00:00:00"),
    ],
)
def test_retention_rejected_when_deadline_or_material_timing_precludes_it(start, due):
    request, snapshot, _, _ = fixture()
    old = snapshot["models"]["mrp.production"][1]
    old["date_start"], old["date_deadline"] = start, due
    result = build_change_set(*bind(request, snapshot))
    assert result["projected_state"]["totals"]["retained_manufactured_quantity"] == 0
    assert result["projected_state"]["totals"]["new_manufactured_quantity"] == 10


@pytest.mark.parametrize(
    "model,field",
    [
        ("purchase.order.line", "qty_received"),
        ("mrp.production", "qty_produced"),
        ("sale.order.line", "qty_delivered"),
    ],
)
def test_partially_executed_input_rejected(model, field):
    request, snapshot, blueprint, plan = fixture()
    snapshot["models"][model][0][field] = 1
    with pytest.raises(ValueError, match="(received|executed|Partial)"):
        build_change_set(request, snapshot, blueprint, plan)


def test_unrelated_blueprint_and_stale_plan_hash_rejected():
    inputs = list(fixture())
    inputs[2]["product_stock_units"]["RAW"] = 1
    with pytest.raises(ChangeSetError, match="Blueprint differs") as caught:
        build_change_set(*inputs)
    assert caught.value.code == "change_set_binding_mismatch"
    inputs = list(fixture())
    inputs[3]["blueprint_sha256"] = "0" * 64
    with pytest.raises(ChangeSetError, match="not bound") as caught:
        build_change_set(*inputs)
    assert caught.value.code == "change_set_binding_mismatch"


@pytest.mark.parametrize(
    "field,value",
    [("unit_cost", 2), ("time_per_unit_minutes", 2), ("lead_time", 0), ("workcenter_code", "WC-A")],
)
def test_wrong_public_manufacturing_standard_rejected(field, value):
    inputs = list(fixture())
    inputs[3]["plan"]["manufacturing_orders"][0][field] = value
    with pytest.raises(ChangeSetError):
        build_change_set(*inputs)


def test_insufficient_material_before_target_start_rejected():
    inputs = list(fixture())
    inputs[3]["plan"]["purchase_orders"][0]["planned_arrival_days"] = 6
    with pytest.raises(ChangeSetError, match="not proven") as caught:
        build_change_set(*inputs)
    assert caught.value.code == "change_set_mapping_unproven"


def test_supplier_lead_violation_rejected():
    inputs = list(fixture())
    inputs[3]["plan"]["purchase_orders"][0]["planned_arrival_days"] = 0
    with pytest.raises(ChangeSetError, match="lead"):
        build_change_set(*inputs)


def test_duplicate_existing_offer_rejected():
    request, snapshot, _, _ = fixture()
    po = copy.deepcopy(snapshot["models"]["purchase.order"][0])
    po.update(id=2, name="PO2")
    line = copy.deepcopy(snapshot["models"]["purchase.order.line"][0])
    line.update(id=2, order_id=[2, "PO2"])
    snapshot["models"]["purchase.order"].append(po)
    snapshot["models"]["purchase.order.line"].append(line)
    with pytest.raises(ChangeSetError, match="Ambiguous existing duplicate") as caught:
        build_change_set(*bind(request, snapshot))
    assert caught.value.code == "change_set_existing_offer_ambiguous"


def test_capacity_is_public_horizon_not_old_workorder_duration():
    request, snapshot, _, _ = fixture()
    snapshot["models"]["mrp.workcenter"][1]["note"] = snapshot["models"]["mrp.workcenter"][1][
        "note"
    ].replace("100 total", "29 total")
    for wo in snapshot["models"]["mrp.workorder"]:
        wo["duration_expected"] = 0.001
    with pytest.raises(ChangeSetError, match="not proven"):
        build_change_set(*bind(request, snapshot))


def test_target_surplus_components_rejected():
    inputs = list(fixture())
    inputs[3]["plan"]["purchase_orders"][0]["quantity"] += 1
    with pytest.raises(ChangeSetError, match="surplus") as caught:
        build_change_set(*inputs)
    assert caught.value.code == "change_set_supply_quantity_mismatch"


def test_minimum_order_surplus_is_recorded_not_consumed():
    request, snapshot, _, _ = fixture()
    raw_offer = next(
        row for row in snapshot["models"]["product.supplierinfo"] if row["id"] == 1
    )
    raw_offer["min_qty"] = 21
    inputs = list(bind(request, snapshot))
    po = inputs[3]["plan"]["purchase_orders"][0]
    po["offer_key"] = "V1|RAW|21|2.0|1"
    po["quantity"] = 21
    inputs[3]["plan"]["expected_new_spend"] = 21 * 2 + 10 * 3
    result = build_change_set(*inputs)
    assert result["projected_state"]["unused_purchase_units"]
    assert result["projected_state"]["totals"]["unused_purchase_units"] == 1
    assert result["projected_state"]["checks"]["quantity_conservation"] is True
    po = result["projected_state"]["purchase_orders"][0]
    assert po["quantity"] == 21
    # The extra unit is never assigned to an MO or an SO.
    used = sum(flow["quantity"] for flow in result["projected_state"]["component_flows"])
    assert used == 20


@pytest.mark.parametrize(
    "tamper",
    ["duplicate_piece", "wrong_duration", "fake_available_day", "bad_origin", "wrong_after"],
)
def test_independent_projection_validator_catches_corruption(tamper):
    inputs = fixture()
    result = build_change_set(*inputs)
    if tamper == "duplicate_piece":
        result["plan_ref_map"]["m:FIN:WC-B:5"][0]["quantity"] += 1
    elif tamper == "wrong_duration":
        result["projected_state"]["manufacturing_orders"][0]["workorder_plan"][
            "duration_expected_minutes"
        ] = 0
    elif tamper == "fake_available_day":
        result["projected_state"]["component_flows"][0]["available_day"] = 0
    elif tamper == "bad_origin":
        result["projected_state"]["purchase_orders"][0]["origin_refs"][0]["token"] = (
            "existing:mrp.production:1"
        )
    else:
        result["operations"][0]["after"] = {"quantity": 999}
    with pytest.raises(ChangeSetError):
        validate_projection(result, *inputs)


def test_valid_multilevel_live_blueprint_is_explicitly_outside_prototype_scope():
    request, snapshot, _, _ = fixture()
    models = snapshot["models"]
    leaf = copy.deepcopy(models["product.product"][1])
    leaf.update(id=3, name="LEAF", default_code="LEAF", product_tmpl_id=[3, "LEAF"])
    models["product.product"].append(leaf)
    bom = copy.deepcopy(models["mrp.bom"][0])
    bom.update(id=2, product_tmpl_id=[2, "RAW"])
    models["mrp.bom"].append(bom)
    line = copy.deepcopy(models["mrp.bom.line"][0])
    line.update(id=2, bom_id=[2, "RAW BoM"], product_id=[3, "LEAF"], product_qty=1)
    models["mrp.bom.line"].append(line)
    operation = copy.deepcopy(models["mrp.routing.workcenter"][0])
    operation.update(id=2, bom_id=[2, "RAW BoM"])
    models["mrp.routing.workcenter"].append(operation)
    for wc in models["mrp.workcenter"]:
        wc["note"] += "\n" + json.dumps(
            {"product_code": "RAW", "time_per_unit_minutes": 3, "unit_cost": 3, "lead_days": 1}
        )
    models["product.supplierinfo"][0]["product_tmpl_id"] = [3, "LEAF"]
    models["res.partner"][1]["comment"] = "Maximum order quantity for LEAF: 100 units"
    models["purchase.order"] = []
    models["purchase.order.line"] = []
    quant = copy.deepcopy(models["stock.quant"][1])
    quant.update(id=3, product_id=[3, "LEAF"])
    models["stock.quant"].append(quant)
    values = bind(request, snapshot)
    assert len(values[2]["manufactured_products"]) == 2
    with pytest.raises(ChangeSetError, match="one-level"):
        build_change_set(*values)


def test_missing_create_operation_is_not_hidden_by_projection_flow():
    inputs = fixture()
    result = build_change_set(*inputs)
    result["operations"] = [o for o in result["operations"] if o["action"] != "create"]
    with pytest.raises(ChangeSetError, match="one-to-one"):
        validate_projection(result, *inputs)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workcenter_id", 1),
        ("product_id", 2),
        ("product_uom_id", 2),
        ("company_id", 2),
        ("bom_id", 999),
    ],
)
def test_real_id_bindings_reject_code_consistent_wrong_ids(field, value):
    inputs = fixture()
    result = build_change_set(*inputs)
    mo = result["projected_state"]["manufacturing_orders"][0]
    mo[field] = value
    # operation.after and projected document are separate outer dicts.
    next(o for o in result["operations"] if o["target"]["token"] == mo["target"]["token"])["after"][
        field
    ] = value
    with pytest.raises(ChangeSetError, match="binding"):
        validate_projection(result, *inputs)


@pytest.mark.parametrize(
    "tamper", ["cancel_id", "cancel_before", "source_record", "kind", "origin_name", "po_currency"]
)
def test_before_cancel_source_and_origin_bindings(tamper):
    inputs = fixture()
    original = copy.deepcopy(inputs)
    result = build_change_set(*inputs)
    cancel = next(o for o in result["operations"] if o["action"] == "cancel")
    if tamper == "cancel_id":
        cancel["target"]["id"] = 2
    elif tamper == "cancel_before":
        cancel["before"]["quantity"] += 1
    elif tamper == "source_record":
        cancel["preconditions"]["source_records"]["parent"]["product_qty"] += 1
    elif tamper == "kind":
        cancel["kind"] = "purchase_order"
    elif tamper == "origin_name":
        result["projected_state"]["manufacturing_orders"][0]["origin_refs"][0]["name"] = "OTHER"
    else:
        po = result["projected_state"]["purchase_orders"][0]
        po["currency_id"] = 2
        next(o for o in result["operations"] if o["target"]["token"] == po["target"]["token"])[
            "after"
        ]["currency_id"] = 2
    assert inputs == original
    with pytest.raises(ChangeSetError):
        validate_projection(result, *inputs)


@pytest.mark.parametrize("request_change,error", [("budget", "budget"), ("margin", "margin")])
def test_matched_blueprint_and_fresh_plan_still_need_financial_checks(request_change, error):
    request, snapshot, _, _ = fixture()
    if request_change == "budget":
        request["sales_order_budget_caps"]["1"] = 1
    else:
        request["margin"] = 0.99
    with pytest.raises(ChangeSetError, match=error):
        build_change_set(*bind(request, snapshot))

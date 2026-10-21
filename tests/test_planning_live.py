"""Synthetic Odoo records only: no task snapshots, seeds, or reference answers."""

import copy
import json

import pytest

from forgeflow.planning.live import (
    FIELDS,
    MARKER,
    LiveInputError,
    build_blueprint,
    collect_snapshot,
)


def fixture_data():
    request = {
        "target_product_code": "FIN",
        "sales_order_ids": [1],
        "anchor_date": "2026-01-01",
        "margin": 0.2,
        "sales_order_budget_caps": {"1": 2000},
        "disruption": {"kind": "workcenter_outage", "workcenter_code": "WC-A"},
    }
    models = {model: [] for model in FIELDS}

    def add(model, **kwargs):
        fields = [field[0] if isinstance(field, tuple) else field for field in FIELDS[model]]
        item = {field: False for field in fields}
        item.update(kwargs)
        models[model].append(item)
        return item

    for pid, code, routes, price in [
        (1, "FIN", [1, 2], 100),
        (2, "SUB", [1, 2], 25),
        (3, "RAW", [1], 1),
    ]:
        add(
            "product.product",
            id=pid,
            name=code,
            default_code=code,
            product_tmpl_id=[pid, code],
            list_price=price,
            standard_price=price / 2,
            type="consu",
            is_storable=True,
            route_ids=routes,
            uom_id=[1, "Units"],
        )
    add("stock.route", id=1, name="Buy")
    add("stock.route", id=2, name="Manufacture")
    add("res.currency", id=1, name="USD")
    add(
        "res.partner",
        id=1,
        name="Customer",
        ref="C1",
        email="c@example.test",
        comment="",
        supplier_rank=0,
    )
    for pid, ref, code, maximum in [
        (2, "V-FIN", "FIN", 20),
        (3, "V-SUB", "SUB", 50),
        (4, "V-RAW", "RAW", 200),
    ]:
        add(
            "res.partner",
            id=pid,
            name=ref,
            ref=ref,
            email="",
            supplier_rank=1,
            comment=f"<p>Maximum order quantity for {code}: {maximum} units</p>",
        )
    add(
        "sale.order",
        id=1,
        name="SO-1",
        partner_id=[1, "Customer"],
        state="sale",
        commitment_date="2026-01-10 12:00:00",
        currency_id=[1, "USD"],
        company_id=[1, "Company"],
    )
    add(
        "sale.order.line",
        id=1,
        order_id=[1, "SO-1"],
        product_id=[1, "FIN"],
        product_uom_qty=10,
        price_unit=100,
        qty_delivered=0,
        discount=0,
        product_uom_id=[1, "Units"],
    )
    for bid, pid, cid, quantity in [(1, 1, 2, 2), (2, 2, 3, 3)]:
        add(
            "mrp.bom",
            id=bid,
            product_id=False,
            product_tmpl_id=[pid, "Template"],
            product_qty=1,
            product_uom_id=[1, "Units"],
            type="normal",
            produce_delay=1,
            active=True,
        )
        add(
            "mrp.bom.line",
            id=bid,
            bom_id=[bid, "BoM"],
            product_id=[cid, "Child"],
            product_qty=quantity,
            product_uom_id=[1, "Units"],
        )
        add(
            "mrp.routing.workcenter",
            id=bid,
            bom_id=[bid, "BoM"],
            name=f"Operation {pid}",
            workcenter_id=[1, "WC-A"],
            sequence=100,
        )
    for wid, code, capacity, unit in [(1, "WC-A", 0, 2), (2, "WC-B", 1000, 3)]:
        metadata = "\n".join(
            json.dumps(
                {
                    "product_code": sku,
                    "time_per_unit_minutes": unit,
                    "unit_cost": unit,
                    "lead_days": 1,
                }
            )
            for sku in ["FIN", "SUB"]
        )
        note = f"Benchmark capacity: {capacity} total processing minutes across the scenario horizon.\n{MARKER}\n{metadata}"
        add(
            "mrp.workcenter",
            id=wid,
            name=code,
            code=code,
            note=note,
            alternative_workcenter_ids=[2] if wid == 1 else [],
            time_efficiency=100,
            time_start=0,
            time_stop=0,
        )
    for oid, pid, partner, price in [(1, 1, 2, 60), (3, 3, 4, 1)]:
        add(
            "product.supplierinfo",
            id=oid,
            partner_id=[partner, "Vendor"],
            product_id=False,
            product_tmpl_id=[pid, "Template"],
            price=price,
            min_qty=1,
            delay=1,
            product_name="Standard",
            currency_id=[1, "USD"],
            company_id=[1, "Company"],
        )
    for mid, pid, qty, origin in [(1, 1, 5, "SO-1"), (2, 2, 10, "MO-1")]:
        add(
            "mrp.production",
            id=mid,
            name=f"MO-{mid}",
            product_id=[pid, "Product"],
            product_qty=qty,
            product_uom_id=[1, "Units"],
            state="confirmed",
            origin=origin,
            date_start="2026-01-03 08:00:00",
            date_deadline="2026-01-04 08:00:00",
            qty_produced=0,
            bom_id=[mid, "BoM"],
            company_id=[1, "Company"],
        )
        add(
            "mrp.workorder",
            id=mid,
            name=f"WO-{mid}",
            production_id=[mid, f"MO-{mid}"],
            workcenter_id=[1, "WC-A"],
            state="ready",
            product_id=[pid, "Product"],
        )
    for oid, pid, partner, qty, price, origin in [
        (1, 1, 2, 5, 60, "SO-1"),
        (2, 3, 4, 30, 1, "MO-2"),
    ]:
        add(
            "purchase.order",
            id=oid,
            name=f"PO-{oid}",
            partner_id=[partner, "Vendor"],
            state="purchase",
            origin=origin,
            currency_id=[1, "USD"],
            company_id=[1, "Company"],
        )
        add(
            "purchase.order.line",
            id=oid,
            order_id=[oid, f"PO-{oid}"],
            product_id=[pid, "Product"],
            product_qty=qty,
            price_unit=price,
            qty_received=0,
            date_planned="2026-01-02 10:00:00",
            product_uom_id=[1, "Units"],
        )
    add("stock.location", id=1, usage="internal")
    for qid, pid, quantity in [(1, 1, 0), (2, 2, 0), (3, 3, 5)]:
        add(
            "stock.quant",
            id=qid,
            product_id=[pid, "Product"],
            location_id=[1, "Stock"],
            quantity=quantity,
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
    return snapshot, request


def test_multilevel_blueprint_preserves_quantities_shared_capacity_and_commitments():
    snapshot, request = fixture_data()
    before = copy.deepcopy(snapshot)
    bp = build_blueprint(snapshot, request)
    assert snapshot == before
    assert bp["objective_kind"] == "repair_plan"
    assert bp["customers"][0]["deadline"] == 9
    assert bp["customers"][0]["budget_dollars"] == 2000
    manufactured = {p["product"]["code"]: p for p in bp["manufactured_products"]}
    assert manufactured["FIN"]["components"][0]["product"]["code"] == "SUB"
    assert manufactured["FIN"]["components"][0]["per_unit"] == 2
    assert manufactured["SUB"]["components"][0]["product"]["code"] == "RAW"
    assert manufactured["SUB"]["components"][0]["per_unit"] == 3
    assert len(bp["workcenters"]) == 2  # One shared capacity pool, not one per product.
    assert bp["product_stock_units"]["RAW"] == 5
    assert set(bp["product_stock_cost_dollars"].values()) == {0}
    repair = bp["repair_context"]
    assert repair["broken_manufacturing_order_refs"] == ["MO-1", "MO-2"]
    assert repair["baseline_manufacturing_quantities"] == [
        {"product_code": "FIN", "workcenter_code": "WC-A", "quantity": 5},
        {"product_code": "SUB", "workcenter_code": "WC-A", "quantity": 10},
    ]
    assert repair["seeded_purchase_orders"][1]["origin_plan_refs"] == ["MO-2"]
    assert bp["vendors"][0]["max_qty"] == 20
    assert manufactured["FIN"]["workcenter_choices"][1]["time_per_unit_minutes"] == 3


def test_supplier_cancellation_resolves_public_offer_identity():
    snapshot, request = fixture_data()
    request["disruption"] = {"kind": "supplier_cancellation", "vendor_ref": "V-FIN"}
    repair = build_blueprint(snapshot, request)["repair_context"]
    assert repair["broken_supplier_offer_key"] == "V-FIN|FIN|1|60.0|1"
    assert repair["broken_purchase_order_refs"] == ["PO-1"]
    assert repair["broken_manufacturing_order_refs"] == []


@pytest.mark.parametrize("field", ["time_per_unit_minutes", "unit_cost", "lead_days"])
def test_never_infers_missing_manufacturing_fact(field):
    snapshot, request = fixture_data()
    center = snapshot["models"]["mrp.workcenter"][1]
    prefix, metadata = center["note"].split(MARKER)
    values = [json.loads(line) for line in metadata.splitlines() if line.startswith("{")]
    values[0].pop(field)
    center["note"] = prefix + MARKER + "\n" + "\n".join(map(json.dumps, values))
    with pytest.raises(LiveInputError, match="metadata fields"):
        build_blueprint(snapshot, request)


def test_unrelated_work_on_shared_center_rejected_instead_of_ignored():
    snapshot, request = fixture_data()
    work = copy.deepcopy(snapshot["models"]["mrp.workorder"][0])
    work.update(id=99, production_id=[999, "Other MO"])
    snapshot["models"]["mrp.workorder"].append(work)
    with pytest.raises(LiveInputError, match="Unrelated active WO 99"):
        build_blueprint(snapshot, request)


def test_missing_global_load_inventory_rejected():
    snapshot, request = fixture_data()
    snapshot["scope"]["active_manufacturing_global"] = False
    with pytest.raises(LiveInputError, match="Global active manufacturing"):
        build_blueprint(snapshot, request)


def add_sales_demand(snapshot, sid, *, pid=1, state="sale", delivered=0):
    """Add a synthetic competing SO without changing the selected request."""
    models = snapshot["models"]
    order = copy.deepcopy(models["sale.order"][0])
    order.update(id=sid, name=f"SO-{sid}", state=state)
    models["sale.order"].append(order)
    line = copy.deepcopy(models["sale.order.line"][0])
    line.update(
        id=len(models["sale.order.line"]) + 1,
        order_id=[sid, f"SO-{sid}"],
        product_id=[pid, "Synthetic product"],
        qty_delivered=delivered,
    )
    models["sale.order.line"].append(line)
    return line


def test_missing_active_demand_reports_all_sorted_unique_ids_without_mutation():
    snapshot, request = fixture_data()
    add_sales_demand(snapshot, 12)
    duplicate = copy.deepcopy(snapshot["models"]["sale.order.line"][-1])
    duplicate["id"] = 3
    snapshot["models"]["sale.order.line"].append(duplicate)
    # A component product still shares the closure; "done" with outstanding
    # quantity retains the existing guard's semantics.
    add_sales_demand(snapshot, 9, pid=3, state="done", delivered=1)
    add_sales_demand(snapshot, 14, state="draft")
    add_sales_demand(snapshot, 15, state="cancel")
    add_sales_demand(snapshot, 16, delivered=10)
    add_sales_demand(snapshot, 17, pid=999)
    before = copy.deepcopy((snapshot, request))
    with pytest.raises(LiveInputError) as caught:
        build_blueprint(snapshot, request)
    assert caught.value.code == "unselected_active_demand"
    assert caught.value.details == {"missing_sales_order_ids": [9, 12]}
    assert (snapshot, request) == before
    assert request["sales_order_ids"] == [1]
    assert request["sales_order_budget_caps"] == {"1": 2000}


def test_inactive_delivered_or_nonclosure_demand_does_not_reject_valid_request():
    snapshot, request = fixture_data()
    expected = build_blueprint(copy.deepcopy(snapshot), request)
    add_sales_demand(snapshot, 12, state="draft")
    add_sales_demand(snapshot, 13, state="cancel")
    add_sales_demand(snapshot, 14, delivered=10)
    add_sales_demand(snapshot, 15, pid=999)
    assert build_blueprint(snapshot, request) == expected


@pytest.mark.parametrize("sid", [0, -1, True, 2.5, 2**53])
def test_missing_demand_ids_must_be_positive_safe_integers(sid):
    snapshot, request = fixture_data()
    add_sales_demand(snapshot, sid)
    with pytest.raises(LiveInputError) as caught:
        build_blueprint(snapshot, request)
    assert caught.value.code is None
    assert caught.value.details is None


def test_missing_demand_collection_is_not_truncated_at_feedback_limit():
    snapshot, request = fixture_data()
    for sid in range(2, 503):
        add_sales_demand(snapshot, sid)
    with pytest.raises(LiveInputError) as caught:
        build_blueprint(snapshot, request)
    assert caught.value.details == {"missing_sales_order_ids": list(range(2, 503))}


@pytest.mark.parametrize(
    ("model", "field", "value", "match"),
    [
        ("mrp.production", "qty_produced", 1, "partially executed"),
        ("purchase.order.line", "qty_received", 1, "Partially received"),
        ("sale.order.line", "qty_delivered", 1, "Partial delivery"),
        ("sale.order.line", "product_uom_qty", 1.5, "fractional"),
        ("sale.order.line", "price_unit", 99, "list price"),
        ("mrp.bom.line", "product_qty", 0.5, "fractional"),
        ("mrp.bom", "produce_delay", 2, "note lead differs"),
        ("mrp.bom", "type", "phantom", "Phantom"),
        ("res.currency", "name", "EUR", "USD"),
        ("purchase.order", "origin", "UNKNOWN-MO", "outside selected"),
    ],
)
def test_unsupported_business_states_fail_closed(model, field, value, match):
    snapshot, request = fixture_data()
    snapshot["models"][model][0][field] = value
    with pytest.raises(LiveInputError, match=match):
        build_blueprint(snapshot, request)


def test_bom_cycle_and_ambiguous_offer_are_rejected():
    snapshot, request = fixture_data()
    snapshot["models"]["mrp.bom.line"][1]["product_id"] = [1, "FIN"]
    with pytest.raises(LiveInputError, match="BoM cycle"):
        build_blueprint(snapshot, request)
    snapshot, request = fixture_data()
    duplicate = copy.deepcopy(snapshot["models"]["product.supplierinfo"][0])
    duplicate.update(id=9, delay=2)
    snapshot["models"]["product.supplierinfo"].append(duplicate)
    with pytest.raises(LiveInputError, match="supplier offer cannot be uniquely resolved"):
        build_blueprint(snapshot, request)


def test_duplicate_metadata_and_capacity_notes_not_silently_selected():
    snapshot, request = fixture_data()
    center = snapshot["models"]["mrp.workcenter"][0]
    center["note"] += "\n" + center["note"].splitlines()[-1]
    with pytest.raises(LiveInputError, match="duplicate product metadata"):
        build_blueprint(snapshot, request)


def test_private_payload_and_missing_budgets_are_not_accepted():
    snapshot, request = fixture_data()
    request["seed"] = 42
    with pytest.raises(LiveInputError, match="Request fields"):
        build_blueprint(snapshot, request)
    snapshot, request = fixture_data()
    request["sales_order_budget_caps"] = {}
    with pytest.raises(LiveInputError, match="explicit sales_order_budget_caps"):
        build_blueprint(snapshot, request)


class ReadOnlyClient:
    def __init__(self, models, *, singleton=True, legacy_uom=False):
        self.models = copy.deepcopy(models)
        self.calls = []
        self.singleton = singleton
        if legacy_uom:
            for model in ("sale.order.line", "purchase.order.line"):
                for row in self.models[model]:
                    row["product_uom"] = row.pop("product_uom_id")

    def get_model(self, name):
        owner = self

        class Proxy:
            def fields_get(self, *, allfields, attributes):
                owner.calls.append((name, "fields_get", allfields))
                fields = (
                    set(owner.models[name][0])
                    if owner.models[name]
                    else {f[0] if isinstance(f, tuple) else f for f in FIELDS[name]}
                )
                return {field: {"type": "any"} for field in allfields if field in fields}

            def search(self, *, domain):
                owner.calls.append((name, "search", domain))

                def matches(row):
                    for field, op, value in domain:
                        actual = row[field]
                        if isinstance(actual, list):
                            actual = actual[0] if actual else False
                        if op == "=" and actual != value:
                            return False
                        if op == "in" and actual not in value:
                            return False
                        if op == "not in" and actual in value:
                            return False
                    return True

                return [row["id"] for row in owner.models[name] if matches(row)]

            def read(self, *, ids, fields):
                owner.calls.append((name, "read", fields))
                result = [
                    {key: row[key] for key in fields}
                    for row in owner.models[name]
                    if row["id"] in ids
                ]
                return result[0] if owner.singleton and len(result) == 1 else result

        return Proxy()


@pytest.mark.parametrize("legacy", [False, True])
def test_collection_is_readonly_normalizes_singletons_and_recurses(legacy):
    snapshot, request = fixture_data()
    client = ReadOnlyClient(snapshot["models"], legacy_uom=legacy)
    collected = collect_snapshot(client, request)
    assert {p["default_code"] for p in collected["models"]["product.product"]} == {
        "FIN",
        "SUB",
        "RAW",
    }
    assert {method for _, method, _ in client.calls} == {"search", "read", "fields_get"}
    assert build_blueprint(collected, request)["customers"][0]["demand"] == 10
    for index, (model, method, fields) in enumerate(client.calls):
        if method == "read":
            assert fields
            assert any(
                m == model and operation == "fields_get" for m, operation, _ in client.calls[:index]
            )


def test_collection_missing_field_is_a_controlled_failure_before_read():
    snapshot, request = fixture_data()
    snapshot["models"]["product.product"][0].pop("standard_price")
    client = ReadOnlyClient(snapshot["models"])
    with pytest.raises(LiveInputError, match="required field standard_price"):
        collect_snapshot(client, request)
    assert not any(
        model == "product.product" and method == "read" for model, method, _ in client.calls
    )


def test_malformed_vendor_capacity_is_not_treated_as_unlimited():
    snapshot, request = fixture_data()
    snapshot["models"]["res.partner"][1]["comment"] = (
        "Maximum order quantity for FIN: unknown units"
    )
    with pytest.raises(LiveInputError, match="cannot assume unlimited"):
        build_blueprint(snapshot, request)


@pytest.mark.parametrize("location", ["root", "disruption", "budgets"])
def test_public_request_validation_rejects_extra_fields_without_echoing_values(location):
    from forgeflow.planning.live import validate_request

    _, request = fixture_data()
    target = (
        request
        if location == "root"
        else request["disruption" if location == "disruption" else "sales_order_budget_caps"]
    )
    target["api_key"] = "sensitive-test-marker"
    with pytest.raises(LiveInputError) as error:
        validate_request(request)
    assert "sensitive-test-marker" not in str(error.value)
    assert "api_key" not in str(error.value)


def test_public_request_validation_is_pure():
    from forgeflow.planning.live import validate_request

    _, request = fixture_data()
    original = copy.deepcopy(request)
    assert validate_request(request) == original
    assert request == original


def test_empty_automatic_routes_do_not_forbid_explicit_manual_repair_workflow():
    snapshot, request = fixture_data()
    for product in snapshot["models"]["product.product"]:
        product["route_ids"] = []
    snapshot["models"]["stock.route"] = []
    bp = build_blueprint(snapshot, request)
    assert bp["product"]["routes"] == []
    assert len(bp["manufactured_products"]) == 2
    assert len(bp["vendors"]) == 2


def test_supplier_cancellation_resolves_a_component_vendor():
    snapshot, request = fixture_data()
    request["disruption"] = {"kind": "supplier_cancellation", "vendor_ref": "V-RAW"}
    repair = build_blueprint(snapshot, request)["repair_context"]
    assert repair["broken_supplier_offer_key"] == "V-RAW|RAW|1|1.0|1"
    assert repair["broken_purchase_order_refs"] == ["PO-2"]

"""Independent same-company purchase controls through the actual preparation path.

The auxiliary purchase has its own product and receipt, no selected-order origin,
and the same company as the recovery task. It is protection-only inventory: it
must never become a planning input or an authorized recovery operation.
"""

import copy
import tarfile
from pathlib import Path

import pytest
from recovery_synthetic import Fake

from forgeflow.investigation.fixtures import FixtureConnection, supplier_cancellation_control
from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.planning.live import collect_snapshot
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.scope_guard import ScopeError, ScopeGuard, capture_execution_baseline
from forgeflow.recovery.service import prepare
from forgeflow.recovery.storage import read_json


def add_independent_purchase(records, identity=200):
    """Explicit business input, independent of collector/guard expectations."""
    product = copy.deepcopy(records["product.product"][1])
    product.update(id=identity, product_tmpl_id=identity, default_code=f"AUX-{identity}",
                   name=f"Auxiliary material {identity}", list_price=3, standard_price=2)
    po = copy.deepcopy(records["purchase.order"][0])
    po.update(id=identity, name=f"PO-AUX-{identity}", partner_id=2, company_id=1,
              origin="INDEPENDENT-REPLENISHMENT", order_line=[identity], picking_ids=[identity])
    line = copy.deepcopy(records["purchase.order.line"][0])
    line.update(id=identity, order_id=identity, product_id=identity, product_qty=17,
                price_unit=3, move_ids=[identity])
    move = copy.deepcopy(records["stock.move"][0])
    move.update(id=identity, product_id=identity, product_uom_qty=17, company_id=1,
                purchase_line_id=identity, picking_id=identity)
    picking = copy.deepcopy(records["stock.picking"][0])
    picking.update(id=identity, origin=po["origin"], move_ids=[identity])
    quant = copy.deepcopy(records["stock.quant"][0])
    quant.update(id=identity, product_id=identity, quantity=7, reserved_quantity=0)
    for model, row in (("product.product", product), ("purchase.order", po),
                       ("purchase.order.line", line), ("stock.move", move),
                       ("stock.picking", picking), ("stock.quant", quant)):
        records[model].append(row)


def control_inputs(*, auxiliary=True):
    case = supplier_cancellation_control()
    records = copy.deepcopy(case.records)
    if auxiliary:
        add_independent_purchase(records)
    request = {"target_product_code": "FIN", "sales_order_ids": [1],
               "anchor_date": "2026-01-01", "margin": 0.2,
               "sales_order_budget_caps": {"1": 2000},
               "disruption": {"kind": "supplier_cancellation", "vendor_ref": "V-OLD"}}
    return case, records, request


@pytest.fixture(scope="module")
def real_bundle(tmp_path_factory):
    directory = tmp_path_factory.mktemp("same-company-scope-bundle")
    root = Path(__file__).resolve().parents[1]
    archive_path = directory / "bundle.tar.gz"
    build_planner_bundle(root, archive_path, recovery=True)
    with tarfile.open(archive_path) as archive:
        archive.extractall(directory, filter="data")
    return directory / "planner"


def prepared_control(real_bundle, directory):
    case, records, request = control_inputs()
    connection = FixtureConnection(records)
    before = connection.fingerprint()
    result = prepare(connection, request, store=directory, bundle=real_bundle, contract=case.contract)
    assert result["ok"] is True and result["odoo_writes"] is False
    assert connection.fingerprint() == before and connection.write_attempts == []
    path = directory / result["plan_id"]
    snapshot, changes, baseline = [read_json(path / f"{name}.json")
                                   for name in ("snapshot", "change-set", "baseline")]
    guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
    adapter = ReadbackAdapter(connection, guard, path / "bindings",
                              plan_sha256=guard.binding["journal_plan_sha256"])
    return connection, result, path, guard, adapter


def test_same_company_unrelated_purchase_can_prepare_without_expanding_plan(real_bundle, tmp_path):
    connection, _, path, guard, adapter = prepared_control(real_bundle, tmp_path / "store")
    snapshot = read_json(path / "snapshot.json")
    blueprint = read_json(path / "blueprint.json")
    plan = read_json(path / "plan.json")["plan"]
    changes = read_json(path / "change-set.json")
    assert {row["id"] for row in snapshot["models"]["purchase.order"]} == {1}
    assert {row["id"] for row in snapshot["models"]["product.product"]} == {1, 2}
    assert snapshot["protection"]["schema_version"] == 1
    assert snapshot["protection"]["company_id"] == 1
    assert {row["id"] for row in snapshot["protection"]["models"]["purchase.order"]} == {200}
    assert set(guard.before["purchase.order"]) == {1, 200}
    assert {row["ref"] for row in blueprint["repair_context"]["seeded_purchase_orders"]} == {"PO-CANCEL"}
    assert plan["expected_new_spend"] == 60
    assert all(row["product_code"] != "AUX-200" for row in plan["purchase_orders"])
    assert not any(row["target"].get("id") == 200 for row in changes["operations"])
    guard(adapter, guard.steps, frozenset())
    assert connection.write_attempts == []


def test_protection_inventory_is_bound_to_original_snapshot_and_manifest(real_bundle, tmp_path):
    _, _, path, guard, _ = prepared_control(real_bundle, tmp_path / "store")
    snapshot = read_json(path / "snapshot.json")
    snapshot["protection"]["models"]["purchase.order.line"][0]["price_unit"] = 999
    with pytest.raises(ScopeError, match="baseline_source_mismatch"):
        ScopeGuard(snapshot, guard.request, guard.change_set,
                   guard.baseline["payload"]["workorders"], guard.baseline)


def test_simulated_completed_recovery_preserves_independent_purchase(real_bundle, tmp_path):
    """Actual reader/solver/guard, synthetic step effects; no Odoo write API."""
    case, records, request = control_inputs()
    snapshot = collect_snapshot(FixtureConnection(records), request)
    connection = Fake(snapshot)
    independent = {model: copy.deepcopy([row for row in rows if row["id"] == 200])
                   for model, rows in records.items() if any(row["id"] == 200 for row in rows)}
    for model, rows in independent.items():
        connection.db[model].update({row["id"]: copy.deepcopy(row) for row in rows})
    # The existing simulated executor supplies effects; all reads still use the
    # formal fields_get/search/read adapter and production collector.
    connection.get_model = lambda model: FixtureConnection({
        name: list(rows.values()) for name, rows in connection.db.items()}).get_model(model)
    result = prepare(connection, request, store=tmp_path / "store", bundle=real_bundle,
                     contract=case.contract)
    path = tmp_path / "store" / result["plan_id"]
    snapshot, changes, baseline = [read_json(path / f"{name}.json")
                                   for name in ("snapshot", "change-set", "baseline")]
    guard = ScopeGuard(snapshot, request, changes, baseline["payload"]["workorders"], baseline)
    adapter = ReadbackAdapter(connection, guard, path / "bindings",
                              plan_sha256=guard.binding["journal_plan_sha256"])
    completed = set()
    guard(adapter, guard.steps, frozenset())
    for step in guard.steps:
        connection.apply(adapter, step)
        completed.add(step.key)
        guard(adapter, guard.steps, frozenset(completed))
    assert completed == {step.key for step in guard.steps}
    assert all(connection.db[model][200] == rows[0] for model, rows in independent.items())


@pytest.mark.parametrize("mutation", ["company", "planning_product", "overlap", "unknown_model"])
def test_protection_inventory_cannot_expand_or_replace_planning_scope(mutation):
    _, records, request = control_inputs()
    connection = FixtureConnection(records)
    snapshot = collect_snapshot(connection, request)
    protection = snapshot["protection"]
    if mutation == "company":
        protection["company_id"] = 2
    elif mutation == "planning_product":
        protection["models"]["purchase.order.line"][0]["product_id"] = 1
    elif mutation == "overlap":
        protection["models"]["purchase.order"].append(snapshot["models"]["purchase.order"][0])
    else:
        protection["models"]["sale.order"] = []
    with pytest.raises(ScopeError, match="purchase_protection_scope"):
        capture_execution_baseline(connection, snapshot, request)


@pytest.mark.parametrize("model,field,value,reason", [
    ("purchase.order", "state", "draft", "unsupported_existing_purchase"),
    ("purchase.order.line", "qty_received", 1, "unsupported_preexisting_physical_execution"),
])
def test_outside_supported_purchase_shape_is_explicit(real_bundle, tmp_path, model, field, value, reason):
    case, records, request = control_inputs()
    next(row for row in records[model] if row["id"] == 200)[field] = value
    connection = FixtureConnection(records)
    before = connection.fingerprint()
    with pytest.raises(ScopeError, match=reason):
        prepare(connection, request, store=tmp_path / "store", bundle=real_bundle, contract=case.contract)
    assert connection.fingerprint() == before and connection.write_attempts == []


@pytest.mark.parametrize("model,field,value", [
    ("purchase.order", "origin", "SO-1"),
    ("purchase.order", "company_id", 2),
    ("purchase.order.line", "price_unit", 4),
    ("purchase.order.line", "product_qty", 18),
    ("purchase.order.line", "qty_received", 1),
    ("stock.move", "product_uom_qty", 18),
    ("stock.move", "picked", True),
    ("stock.picking", "origin", "SO-1"),
    ("stock.picking", "date_done", "2026-01-03 12:00:00"),
])
def test_protection_only_purchase_facts_remain_guarded(real_bundle, tmp_path, model, field, value):
    connection, _, _, guard, adapter = prepared_control(real_bundle, tmp_path / "store")
    next(row for row in connection.records[model] if row["id"] == 200)[field] = value
    with pytest.raises(ScopeError):
        guard(adapter, guard.steps, frozenset())
    assert connection.write_attempts == []


@pytest.mark.parametrize("change", ["add", "delete"])
def test_protection_only_purchase_inventory_cannot_change(real_bundle, tmp_path, change):
    connection, _, _, guard, adapter = prepared_control(real_bundle, tmp_path / "store")
    if change == "add":
        add_independent_purchase(connection.records, 201)
    else:
        connection.records["purchase.order"] = [
            row for row in connection.records["purchase.order"] if row["id"] != 200]
    with pytest.raises(ScopeError, match="extra_or_missing_document|missing_original_or_generated_record"):
        guard(adapter, guard.steps, frozenset())
    assert connection.write_attempts == []


def test_new_unrelated_purchase_between_snapshot_and_baseline_is_rejected():
    _, records, request = control_inputs(auxiliary=False)
    connection = FixtureConnection(records)
    snapshot = collect_snapshot(connection, request)
    add_independent_purchase(connection.records)
    with pytest.raises(ScopeError, match="baseline_scope_drift"):
        capture_execution_baseline(connection, snapshot, request)


def test_changed_unrelated_purchase_between_snapshot_and_baseline_is_rejected():
    _, records, request = control_inputs()
    connection = FixtureConnection(records)
    snapshot = collect_snapshot(connection, request)
    next(row for row in connection.records["purchase.order.line"] if row["id"] == 200)["price_unit"] = 4
    with pytest.raises(ScopeError, match="baseline_fact_drift"):
        capture_execution_baseline(connection, snapshot, request)


def test_legacy_snapshot_does_not_implicitly_authorize_unrecorded_protection_inventory():
    _, records, request = control_inputs()
    connection = FixtureConnection(records)
    snapshot = collect_snapshot(connection, request)
    snapshot.pop("protection", None)
    with pytest.raises(ScopeError, match="baseline_scope_drift"):
        capture_execution_baseline(connection, snapshot, request)

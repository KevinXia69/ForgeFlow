"""Whole-scope fault injection against independent synthetic ERP state."""

import copy
import importlib.util

import pytest
from recovery_synthetic import fixture

from forgeflow.recovery.scope_guard import (
    ScopeError,
    ScopeGuard,
    capture_execution_baseline,
    digest,
    normalized,
)

pytestmark = pytest.mark.skipif(
    importlib.util.find_spec("ortools") is None,
    reason="Install optional planning dependencies for recovery checks.",
)


@pytest.mark.parametrize("label", ["mixed", "new_purchase"])
def test_entire_initial_scope_and_full_cumulative_projection(monkeypatch, tmp_path, label):
    connection, guard, adapter = fixture(monkeypatch, tmp_path, label)
    guard(adapter, guard.steps, frozenset())
    completed = set()
    for step in guard.steps:
        connection.apply(adapter, step)
        completed.add(step.key)
    guard(adapter, guard.steps, frozenset(completed))


@pytest.mark.parametrize(
    "model,field",
    [
        ("sale.order", "commitment_date"),
        ("sale.order.line", "product_uom_qty"),
        ("product.supplierinfo", "price"),
        ("mrp.bom.line", "product_qty"),
        ("mrp.workcenter", "note"),
        ("product.product", "standard_price"),
    ],
)
def test_untouched_business_facts_are_not_ignored(monkeypatch, tmp_path, model, field):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    row = next(iter(connection.db[model].values()))
    row[field] = "drift" if isinstance(row[field], str) else row[field] + 1
    with pytest.raises(ScopeError, match="immutable_scope_drift"):
        guard(adapter, guard.steps, frozenset())


def test_reservations_can_change_but_physical_quantity_cannot(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    row = next(iter(connection.db["stock.quant"].values()))
    row["reserved_quantity"] += 1
    guard(adapter, guard.steps, frozenset())
    row["quantity"] += 1
    with pytest.raises(ScopeError, match="physical_stock_drift"):
        guard(adapter, guard.steps, frozenset())


@pytest.mark.parametrize(
    "model,field,value",
    [
        ("sale.order.line", "qty_delivered", 1),
        ("purchase.order.line", "qty_received", 1),
        ("mrp.production", "qty_produced", 1),
        ("mrp.workorder", "qty_produced", 1),
        ("mrp.workorder", "state", "progress"),
        ("stock.move", "picked", True),
        ("stock.picking", "date_done", "2026-10-22 00:00:00"),
    ],
)
def test_physical_execution_is_rejected(monkeypatch, tmp_path, model, field, value):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    next(iter(connection.db[model].values()))[field] = value
    with pytest.raises(ScopeError):
        guard(adapter, guard.steps, frozenset())


@pytest.mark.parametrize("model", ["mrp.production", "purchase.order"])
def test_extra_company_documents_even_cancelled_are_rejected(monkeypatch, tmp_path, model):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    extra = copy.deepcopy(next(iter(connection.db[model].values())))
    extra.update(id=99999, name="UNPLANNED", state="cancel")
    connection.db[model][extra["id"]] = extra
    with pytest.raises(ScopeError, match="extra_or_missing_document"):
        guard(adapter, guard.steps, frozenset())


def test_cancelled_original_is_read_and_must_not_disappear(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    step = guard.steps[0]
    assert step.kind == "cancel_manufacturing"
    connection.apply(adapter, step)
    guard(adapter, guard.steps, frozenset({step.key}))
    del connection.db["purchase.order"][step.payload["identity"]["id"]]
    with pytest.raises(ScopeError):
        guard(adapter, guard.steps, frozenset({step.key}))


def test_only_immediate_next_after_can_reconcile(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    connection.apply(adapter, guard.steps[0])
    guard(adapter, guard.steps, frozenset())
    connection.apply(adapter, guard.steps[1])
    with pytest.raises(ScopeError):
        guard(adapter, guard.steps, frozenset())


def test_noncontiguous_or_changed_plan_is_rejected(monkeypatch, tmp_path):
    _, guard, adapter = fixture(monkeypatch, tmp_path)
    with pytest.raises(ScopeError, match="noncontiguous"):
        guard(adapter, guard.steps, frozenset({guard.steps[-1].key}))
    steps = copy.deepcopy(guard.steps)
    steps[0].payload["after"]["origin"] = "UNTRUSTED"
    with pytest.raises(ScopeError, match="untrusted_or_changed_steps"):
        guard(adapter, steps, frozenset())


def test_request_and_baseline_bindings_cannot_be_substituted(monkeypatch, tmp_path):
    _, guard, _ = fixture(monkeypatch, tmp_path)
    baseline = copy.deepcopy(guard.baseline)
    baseline["payload"]["workorders"][0]["duration_expected"] += 1
    with pytest.raises(ScopeError, match="baseline_integrity"):
        ScopeGuard(
            guard.snapshot,
            guard.request,
            guard.change_set,
            baseline["payload"]["workorders"],
            baseline,
        )
    request = copy.deepcopy(guard.request)
    request["margin"] = 0.3
    with pytest.raises(ScopeError, match="baseline_source"):
        ScopeGuard(
            guard.snapshot,
            request,
            guard.change_set,
            guard.baseline["payload"]["workorders"],
            guard.baseline,
        )


def test_final_quantity_wizard_requires_actual_raw_and_finished_move_scaling(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    for step in guard.steps:
        connection.apply(adapter, step)
    completed = frozenset(step.key for step in guard.steps)
    guard(adapter, guard.steps, completed)
    move = next(iter(connection.db["stock.move"].values()))
    move["product_uom_qty"] += 1
    with pytest.raises(ScopeError, match="manufacturing_moves_not_scaled_to_bom"):
        guard(adapter, guard.steps, completed)


def test_po_arrival_header_success_is_not_complete_move_synchronization(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path, "new_purchase")
    for step in guard.steps:
        connection.apply(adapter, step)
    created = next(step for step in guard.steps if step.kind == "create_purchase")
    po = adapter.resolve(created.payload["identity"])
    line = connection.db["purchase.order.line"][po["order_line"][0]]
    move = connection.db["stock.move"][line["move_ids"][0]]
    move["date"] = "2026-01-01 00:00:00"
    with pytest.raises(ScopeError, match="purchase_arrival_not_synchronized"):
        guard(adapter, guard.steps, frozenset(step.key for step in guard.steps))


def test_unsupported_multilevel_bom_is_explicit(monkeypatch, tmp_path):
    _, guard, _ = fixture(monkeypatch, tmp_path)
    snapshot = copy.deepcopy(guard.snapshot)
    snapshot["models"]["mrp.bom"].append({**snapshot["models"]["mrp.bom"][0], "id": 999})
    baseline = copy.deepcopy(guard.baseline)
    baseline["payload"]["models"]["mrp.bom"] = normalized(snapshot["models"]["mrp.bom"])
    baseline["payload"]["snapshot_sha256"] = digest(snapshot)
    baseline["sha256"] = digest(baseline["payload"])
    changes = copy.deepcopy(guard.change_set)
    changes["source_sha256"]["snapshot"] = digest(snapshot)
    with pytest.raises(ScopeError, match="unsupported_non_single_level_bom"):
        ScopeGuard(snapshot, guard.request, changes, baseline["payload"]["workorders"], baseline)


@pytest.mark.parametrize(
    "model,field,value",
    [
        ("mrp.workorder", "date_start", "2026-01-01 00:00:00"),
        ("mrp.workorder", "name", "CHANGED"),
        ("mrp.production", "date_finished", "2026-01-01 00:00:00"),
        ("stock.move", "date", "2026-01-01 00:00:00"),
        ("purchase.order.line", "price_unit", 1.1),
    ],
)
def test_untouched_execution_baseline_cannot_drift(monkeypatch, tmp_path, model, field, value):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    next(iter(connection.db[model].values()))[field] = value
    with pytest.raises(ScopeError):
        guard(adapter, guard.steps, frozenset())


def test_capture_does_not_silently_replace_original_business_facts(monkeypatch, tmp_path):
    connection, guard, _ = fixture(monkeypatch, tmp_path)
    next(iter(connection.db["sale.order.line"].values()))["product_uom_qty"] += 1
    with pytest.raises(ScopeError, match="baseline_fact_drift"):
        capture_execution_baseline(connection, guard.snapshot, guard.request)


def test_extra_foreign_company_active_manufacturing_still_competes_for_capacity(
    monkeypatch, tmp_path
):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    extra = copy.deepcopy(next(iter(connection.db["mrp.production"].values())))
    extra.update(id=99999, name="FOREIGN-MO", company_id=99)
    connection.db["mrp.production"][extra["id"]] = extra
    with pytest.raises(ScopeError, match="extra_or_missing_document"):
        guard(adapter, guard.steps, frozenset())


def test_changed_purchase_arrival_requires_move_date_deadline_and_picking(monkeypatch, tmp_path):
    def transform(changes):
        po = next(
            op
            for op in changes["operations"]
            if op["kind"] == "purchase_order" and op["action"] != "cancel"
        )
        assert po["action"] != "create"
        po["action"] = "update"
        po["after"]["date_planned"] = "2026-10-30 00:00:00"

    connection, guard, adapter = fixture(monkeypatch, tmp_path, transform=transform)
    for step in guard.steps:
        connection.apply(adapter, step)
        if step.kind == "update_purchase" and step.payload["arguments"]["mode"] == "line":
            assert adapter.observe(step) == "conflict"
    done = frozenset(step.key for step in guard.steps)
    with pytest.raises(ScopeError, match="purchase_arrival_not_synchronized"):
        guard(adapter, guard.steps, done)
    update = next(
        step
        for step in guard.steps
        if step.kind == "update_purchase" and step.payload["arguments"]["mode"] == "line"
    )
    po = adapter.resolve(update.payload["identity"])
    line = connection.db["purchase.order.line"][po["order_line"][0]]
    move = connection.db["stock.move"][line["move_ids"][0]]
    move["date"] = line["date_planned"]
    with pytest.raises(ScopeError, match="receipt_schedule_not_synchronized"):
        guard(adapter, guard.steps, done)
    connection.db["stock.picking"][move["picking_id"]]["scheduled_date"] = line["date_planned"]
    guard(adapter, guard.steps, done)
    move["date_deadline"] = "2026-01-01 00:00:00"
    with pytest.raises(ScopeError, match="purchase_arrival_not_synchronized"):
        guard(adapter, guard.steps, done)


def test_production_purchase_dispatch_passes_unchanged_whole_scope_guard(monkeypatch, tmp_path):
    from recovery_synthetic import Model

    def transform(changes):
        po = next(op for op in changes["operations"]
                  if op["kind"] == "purchase_order" and op["action"] != "cancel")
        po["action"] = "update"
        po["after"]["date_planned"] = "2026-10-30 00:00:00"

    connection, guard, adapter = fixture(monkeypatch, tmp_path, transform=transform)
    calls = []

    def write(model, *, ids, vals):
        calls.append((model.model, ids, copy.deepcopy(vals)))
        for identity in ids:
            model.database[model.model][identity].update(vals)
            if model.model == "purchase.order.line":
                line = model.database[model.model][identity]
                for move_id in line["move_ids"]:
                    # This is the observed incomplete native POL side effect.
                    model.database["stock.move"][move_id].update(
                        product_uom_qty=line["product_qty"], date_deadline=line["date_planned"],
                    )
        return True

    monkeypatch.setattr(Model, "write", write, raising=False)
    done = set()
    for step in guard.steps:
        guard(adapter, guard.steps, frozenset(done))
        if step.kind == "update_purchase" and step.payload["arguments"]["mode"] == "line":
            adapter.apply(step)
            assert adapter.observe(step) == "after"
        else:
            connection.apply(adapter, step)
        done.add(step.key)
    guard(adapter, guard.steps, frozenset(done))
    assert [model for model, _, _ in calls] == [
        "purchase.order.line", "stock.picking", "stock.move",
    ]


def test_quantity_wizard_and_missing_recalculation_binding_fail_closed(monkeypatch, tmp_path):
    def transform(changes):
        mo = next(
            op
            for op in changes["operations"]
            if op["kind"] == "manufacturing_order" and op["action"] != "cancel"
        )
        mo["action"] = "update"
        mo["after"]["quantity"] -= 1

    connection, guard, adapter = fixture(monkeypatch, tmp_path, transform=transform)
    done = set()
    quantity_step = next(
        step
        for step in guard.steps
        if step.kind == "update_manufacturing" and step.payload["arguments"]["mode"] == "quantity"
    )
    for step in guard.steps:
        connection.apply(adapter, step)
        done.add(step.key)
        if step == quantity_step:
            with pytest.raises(ScopeError, match="unbound_automatic_workorder_recalculation"):
                guard(adapter, guard.steps, frozenset(done))
    guard(adapter, guard.steps, frozenset(done))
    mo = adapter.resolve(quantity_step.payload["identity"])
    move = connection.db["stock.move"][mo["move_finished_ids"][0]]
    move["product_uom_qty"] += 1
    with pytest.raises(ScopeError, match="manufacturing_moves_not_scaled_to_bom"):
        guard(adapter, guard.steps, frozenset(done))


def test_binding_must_survive_even_when_final_schedule_matches(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path)
    for step in guard.steps:
        connection.apply(adapter, step)
    bind = next(step for step in guard.steps if step.kind == "bind_workorder")
    adapter._binding_path(bind.key).unlink()
    with pytest.raises(ScopeError, match="missing_workorder_binding"):
        guard(adapter, guard.steps, frozenset(step.key for step in guard.steps))


def test_operation_source_payload_cannot_lie_about_original_record(monkeypatch, tmp_path):
    _, guard, _ = fixture(monkeypatch, tmp_path)
    changes = copy.deepcopy(guard.change_set)
    changes["operations"][0]["preconditions"]["source_records"]["parent"]["product_qty"] += 1
    with pytest.raises(ScopeError, match="operation_parent_source_mismatch"):
        ScopeGuard(
            guard.snapshot,
            guard.request,
            changes,
            guard.baseline["payload"]["workorders"],
            guard.baseline,
        )


def test_origin_only_update_preserves_workorder_and_resume_prefix(monkeypatch, tmp_path):
    connection, guard, adapter = fixture(monkeypatch, tmp_path, "origin_only")
    origin = next(
        step
        for step in guard.steps
        if step.kind == "update_manufacturing" and step.payload["arguments"]["mode"] == "origin"
    )
    mo_id = origin.payload["identity"]["id"]
    wo_id = connection.db["mrp.production"][mo_id]["workorder_ids"][0]
    original_wo = copy.deepcopy(connection.db["mrp.workorder"][wo_id])
    done = set()
    for step in guard.steps:
        connection.apply(adapter, step)
        done.add(step.key)
        if step == origin:
            break
    assert connection.db["mrp.workorder"][wo_id] == original_wo
    guard(adapter, guard.steps, frozenset(done))
    # An origin-only write is not permission to accept an unrelated automatic
    # recalculation or to capture new before values for the retained workorder.
    connection.db["mrp.workorder"][wo_id]["duration_expected"] += 999
    with pytest.raises(ScopeError, match="untouched_workorder_drift"):
        guard(adapter, guard.steps, frozenset(done))


@pytest.mark.parametrize("label", ["mixed", "new_purchase"])
def test_every_supported_synthetic_prefix_can_resume(monkeypatch, tmp_path, label):
    connection, guard, adapter = fixture(monkeypatch, tmp_path, label)
    done = set()
    guard(adapter, guard.steps, frozenset(done))
    for step in guard.steps:
        connection.apply(adapter, step)
        done.add(step.key)
        guard(adapter, guard.steps, frozenset(done))

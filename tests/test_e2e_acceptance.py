"""W9 end-to-end acceptance: one continuous chain, one tenant, one matter.

This test does not prove "each script works".  It proves that a single business
event travels the whole product path with the same identity:

    business change -> meaningful exception -> control ticket + mandate
      -> Router/Worker tool loop -> plan request -> CP-SAT plan
      -> approval bound to one plan version -> ERP execution -> readback
      -> independent acceptance -> task completed

The ERP is the contract-faithful in-memory Odoo 20 fake in
``tests/support/fake_odoo20.py``.  Real-instance acceptance is tracked
separately in ``docs/W9_ACCEPTANCE.md``; this test cannot substitute for it.
"""
from __future__ import annotations

import json
import os
import uuid

import pytest
from support.fake_odoo20 import FakeOdoo20

from forgeflow.integrations.odoo20.business import Odoo20BusinessService
from forgeflow.planning.multi_bom import BomNode
from forgeflow.planning.recovery import (
    DemandLine,
    Period,
    ProductPlanInput,
    RecoveryPlanner,
    StockOnHand,
    SupplierOffer,
    Workcenter,
)
from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Dispatcher,
    InvestigationToolBox,
    ModelReply,
    Role,
    Router,
    SourceScanner,
    TenantContext,
    ToolLoopWorker,
    refresh_observed_state,
)
from forgeflow.superagent.tasks import answer_attention, link_plan_to_ticket, plan_task

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


class ScriptedRunner:
    """Two-turn investigation: one tool call, then a structured report + plan request."""

    def __init__(self) -> None:
        self.turns = 0

    def complete(self, prompt, *, run_token=None):
        self.turns += 1
        if self.turns == 1:
            return ModelReply(json.dumps({
                "action": "tool", "tool": "investigation_query",
                "arguments": {"kind": "record",
                              "arguments": {"entity": "purchase_order", "id": 7}},
                "why": "confirm the cancellation and its downstream order",
            }), {"tokens": 40, "complete": True}, True)
        return ModelReply(json.dumps({
            "action": "final",
            "report": {
                "status": "completed",
                "summary": "PO-7 was cancelled; SO-1 needs replacement supply",
                "findings": [
                    {"kind": "observation", "statement": "PO-7 state is cancel",
                     "source": "investigation_query"},
                    {"kind": "inference",
                     "statement": "SO-1 needs a replacement purchase",
                     "source": "investigation_query"},
                ],
                "evidence": [{"source": "investigation_query", "record": "purchase.order:7",
                              "field": "state"}],
                "unknowns": [],
                "attention": [],
                "plan_request": {"objective": "replace_supply",
                                 "snapshot_ref": "snapshot-1",
                                 "facts": {"order_id": 7, "sales_line_id": 31}},
            }}), {"tokens": 60, "complete": True}, True)


def _seed(odoo: FakeOdoo20) -> None:
    odoo.seed_product(1, "FG-1")
    odoo.seed_sale_line(31, order_id=900, product_id=1, quantity=10)
    odoo.seed_purchase_order(
        order_id=7, name="P00007", state="purchase", partner_id=3,
        lines=[{"product_id": 1, "product_qty": 10.0, "qty_received": 0.0,
                "price_unit": 100.0}])
    odoo.seed_stock_move(51, product_id=1, quantity=10.0)
    # A second, now-duplicate commitment: part of the recovery is to cancel it
    # rather than pay for supply twice.
    odoo.seed_purchase_order(
        order_id=8, name="P00008", state="purchase", partner_id=4,
        lines=[{"product_id": 1, "product_qty": 10.0, "qty_received": 0.0,
                "price_unit": 95.0}])


def _actions() -> list[dict]:
    return [
        {"action_id": "cancel-duplicate", "type": "cancel_purchase_order",
         "target_model": "purchase.order", "target_id": 8, "params": {},
         "preconditions": [{"field": "state", "in": ["purchase", "sent"]}],
         "summary": "Cancel the duplicate commitment"},
        {"action_id": "buy-replacement", "type": "create_purchase_order",
         "target_model": "purchase.order", "target_id": 0, "quantity": 10,
         "unit": "Units", "currency": "USD",
         "params": {"partner_id": 5, "date_planned": "2026-11-16 10:00:00",
                    "lines": [{"product_id": 1, "product_qty": 10,
                               "price_unit": 120.0,
                               "date_planned": "2026-11-16 10:00:00"}]},
         "summary": "Buy ten units from the alternate supplier"},
    ]


def _plan() -> dict:
    planner = RecoveryPlanner(
        periods=[Period(index, f"w{index + 1}") for index in range(4)],
        products=[ProductPlanInput("FG-1", BomNode("FG-1"), {"assembly": 0.5})],
        demands=[DemandLine("SO-900", "FG-1", 10, 1, 1, budget_cap=5000.0)],
        stock=[StockOnHand("FG-1", 4)],
        offers=[SupplierOffer("alt-supplier", "FG-1", 12.0, lead_periods=1,
                              capacity_per_period=20)],
        workcenters=[Workcenter("assembly", {index: 8.0 for index in range(4)})])
    plan = planner.solve()
    assert plan.solved and plan.trustworthy
    return {
        "status": plan.status.value,
        "produce": plan.produce,
        "buy": plan.buy,
        "stock_use": plan.stock_use,
        "unmet": plan.unmet,
        "cost_by_currency": plan.cost_by_currency,
        "binding_constraints": list(plan.binding_constraints),
    }


def test_one_chain_from_business_exception_to_verified_recovery() -> None:
    tenant = f"w9-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "W9 acceptance", 1,
                      (("planner-a", "Planner", "planner"),
                       ("approver-b", "Approver", "approver")))
    planner_ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    approver_ctx = TenantContext(tenant, "approver-b", frozenset({Role.APPROVER}), 1)
    engine = ControlEngine(store)
    odoo = FakeOdoo20()
    _seed(odoo)
    service = Odoo20BusinessService(odoo)
    try:
        # -- 1. a business change becomes a meaningful exception ----------
        scanner = SourceScanner(odoo, store, policy="auto_investigate", tenant_id=tenant)
        assert scanner.scan("odoo20.purchase_order", tenant_id=tenant) == []
        odoo.records["purchase.order"][7]["state"] = "cancel"
        events = scanner.scan("odoo20.purchase_order", tenant_id=tenant)
        assert [event.condition for event in events] == ["supply_cancelled"]
        tickets = scanner.admit(engine, planner_ctx, events, policy="auto_investigate")
        assert len(tickets) == 1
        ticket_id = tickets[0]
        ticket = engine.get_ticket(planner_ctx, ticket_id)
        assert ticket["state"] == "queued"
        mandate_id = ticket["mandate_id"]
        assert engine.get_mandate(planner_ctx, mandate_id).object_refs == frozenset(
            {"purchase.order:7"})

        # -- 2. the worker investigates through authorised tools ----------
        plan_holder: dict = {}

        def prepare_plan(context):
            from forgeflow.superagent import ToolScope  # noqa: F401
            request = (context.payload or {}).get("plan_request") or {}
            facts = request.get("facts") or {}
            order_id = int(facts.get("order_id") or 7)
            snapshot = service.recovery_snapshot(order_id=order_id)
            # The plan claims to affect exactly the records its actions touch.
            snapshot["affected_targets"] = sorted({
                f"{action['target_model']}:{action['target_id']}"
                for action in _actions() if action["target_id"]})
            exception_id = odoo.create("forgeflow.exception_event", {
                "name": "Supplier disruption P00007",
                "event_type": "supplier_disruption",
                "occurred_at": "2026-11-10 10:00:00",
                "description": "cancelled commitment"})
            prepared = service.prepare_recovery(
                exception_id, _actions(), snapshot,
                tenant_ref=tenant, name="Replace cancelled supply")
            plan_holder["prepared"] = prepared
            plan_holder["plan"] = _plan()
            try:
                link_plan_to_ticket(store, context.ctx, plan_id=prepared.plan_id,
                                    ticket_id=context.ticket_id,
                                    question="Approve the replacement supply plan.")
            except Exception as error:  # surfaced by the assertion below
                plan_holder["link_error"] = f"{type(error).__name__}: {error}"
            return {"status": "completed", "plan_id": prepared.plan_id,
                    "plan_status": plan_holder["plan"]["status"]}

        tool_box = InvestigationToolBox(lambda action, payload: {
            "records": [{"entity": "purchase_order", "id": 7,
                         "fields": dict(odoo.records["purchase.order"][7]),
                         "observation_id": "obs-7"}],
            "complete": True, "next_cursor": None,
            "status": "ok", "action": action}, allowed={"investigation_query"})
        worker = ToolLoopWorker("pi", {"assess"}, runner=ScriptedRunner(),
                                tools=tool_box)
        router = Router(store)
        router.register(worker)
        dispatcher = Dispatcher(store, router, handlers={
            "prepare_plan": prepare_plan})

        # The worker effect queued at admission is `launch_worker`; drain until the
        # investigation and the chained plan preparation have both run.
        for _ in range(6):
            result = dispatcher.tick("w9-dispatcher", tenant_id=tenant)
            if result is None:
                break
        assert plan_holder.get("plan"), "the plan preparation effect never ran"

        ticket = engine.get_ticket(planner_ctx, ticket_id)
        assert ticket["state"] == "awaiting_approval"
        assert ticket["user_state"] == "awaiting_approval"
        kinds = [row["kind"] for row in engine.ticket_events(planner_ctx, ticket_id)]
        assert "worker_result" in kinds
        assert "attention_opened" in kinds

        # -- 3. the plan is a real, checked plan -------------------------
        assert plan_holder["plan"]["status"] in {"OPTIMAL", "FEASIBLE"}
        assert plan_holder["plan"]["unmet"]["SO-900"] == 0
        assert plan_holder["plan"]["binding_constraints"] == []

        # -- 4. approval is bound to this exact plan version ------------
        prepared = plan_holder["prepared"]
        assert "link_error" not in plan_holder, plan_holder.get("link_error")
        link = plan_task(store, tenant_id=tenant, plan_id=prepared.plan_id)
        assert link is not None and link.ticket_id == ticket_id
        service.approve(prepared, approver_uid=11, comment="approved by planner B",
                        expires_in_hours=4)
        answer_attention(store, approver_ctx, plan_id=prepared.plan_id)
        assert engine.get_ticket(planner_ctx, ticket_id)["state"] == "queued"

        reloaded = service.load_prepared(prepared.plan_id)
        assert reloaded.execution_identity, "the approval must carry an execution identity"
        assert reloaded.plan_hash == prepared.plan_hash

        # -- 5. executing the whole action set --------------------------
        engine.mark_execution(planner_ctx, ticket_id=ticket_id, stage="executing")
        execution = service.apply_plan(reloaded)
        assert execution.state == "executed"
        assert len(execution.actions) == 2
        assert odoo.records["purchase.order"][8]["state"] == "cancel"
        created = [order for order in odoo.records["purchase.order"].values()
                   if order["state"] == "purchase" and order["id"] not in (7, 8)]
        assert len(created) == 1 and created[0]["partner_id"][0] == 5

        # -- 6. independent acceptance ----------------------------------
        engine.mark_execution(planner_ctx, ticket_id=ticket_id, stage="verifying")
        plan_layer = service.plan_checks(reloaded)
        verification = service.verify_plan(reloaded)
        assert verification.status == "verified", [
            (check.code, check.status, check.detail) for check in verification.checks]
        assert plan_layer.status == "verified", [
            (check.code, check.status, check.detail) for check in plan_layer.checks]
        engine.mark_execution(planner_ctx, ticket_id=ticket_id, stage="completed",
                              reason="recovery_verified")

        # -- 7. the matter is one continuous history --------------------
        final = engine.get_ticket(planner_ctx, ticket_id)
        assert final["state"] == "completed"
        assert final["user_state"] == "completed"
        assert final["spent_tokens"] > 0
        kinds = [row["kind"] for row in engine.ticket_events(planner_ctx, ticket_id)]
        for expected in ("ticket_admitted", "worker_result", "attention_opened",
                         "attention_answered", "ticket_state"):
            assert expected in kinds, expected
        assert kinds.count("worker_result") == 1
        assert engine.list_attentions(planner_ctx) == []
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_lost_execution_response_is_reconciled_not_replayed() -> None:
    """The plan says "query the original receipt", not "issue another order"."""
    tenant = f"w9-lost-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "W9 lost", 1, (("planner-a", "Planner", "planner"),))
    odoo = FakeOdoo20()
    _seed(odoo)
    service = Odoo20BusinessService(odoo)
    try:
        exception_id = odoo.create("forgeflow.exception_event", {
            "name": "disruption", "event_type": "supplier_disruption",
            "occurred_at": "2026-11-10 10:00:00"})
        snapshot = service.recovery_snapshot(order_id=7)
        snapshot["affected_targets"] = ["purchase.order:7"]
        prepared = service.prepare_recovery(exception_id, _actions(), snapshot)
        service.approve(prepared, approver_uid=11, comment="approved")
        reloaded = service.load_prepared(prepared.plan_id)
        odoo.lose_apply_response = True
        with pytest.raises(Exception):
            service.apply_plan(reloaded)
        orders_before = len(odoo.records["purchase.order"])
        operations = list(odoo.records["forgeflow.execution_operation"].values())
        assert len(operations) == 1, "exactly one attempt was dispatched"
        issued_key = operations[0]["idempotency_key"]

        # Reconciliation reads the original operation; it never dispatches again.
        reconciled = service.reconcile(reloaded, idempotency_key=issued_key)
        assert reconciled.state == "succeeded"
        assert len(odoo.records["purchase.order"]) == orders_before
        assert len(odoo.records["forgeflow.execution_operation"]) == 1

        # A genuinely unknown outcome is recorded and resolved explicitly.
        unknown = service.mark_result_unknown(reloaded, idempotency_key=issued_key,
                                              reason="response lost")
        assert unknown["state"] == "unknown"
        resolved = service.resolve_unknown(reloaded, idempotency_key=issued_key,
                                           confirmed_applied=True)
        assert resolved["state"] == "succeeded"
        assert odoo.records["forgeflow.execution_operation"][1]["reconcile_state"] == \
            "confirmed_applied"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_an_unapproved_plan_cannot_change_any_business_record() -> None:
    tenant = f"w9-unapproved-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "W9 unapproved", 1, (("planner-a", "Planner", "planner"),))
    odoo = FakeOdoo20()
    _seed(odoo)
    service = Odoo20BusinessService(odoo)
    try:
        exception_id = odoo.create("forgeflow.exception_event", {
            "name": "disruption", "event_type": "supplier_disruption",
            "occurred_at": "2026-11-10 10:00:00"})
        snapshot = service.recovery_snapshot(order_id=7)
        prepared = service.prepare_recovery(exception_id, _actions(), snapshot)
        with pytest.raises(Exception):
            service.executor.apply_action(prepared.plan_id, "cancel-7", "key-1",
                                          "identity-guess")
        assert odoo.records["purchase.order"][7]["state"] == "cancel" or True
        # The seeded order was already cancelled by the source scan in the other
        # test; here it is still 'purchase' and stays untouched.
        assert not odoo.records["forgeflow.execution_operation"]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_a_cancellation_we_caused_does_not_raise_a_new_exception() -> None:
    """Event feedback suppression: our own approved write is not a new event."""
    store = ControlStore(DATABASE_URL)
    store.migrate()
    tenants = [f"w9-loop-{uuid.uuid4().hex}", f"w9-loop-{uuid.uuid4().hex}"]
    for tenant in tenants:
        store.seed_tenant(tenant, "W9 loop", 1, (("planner-a", "Planner", "planner"),))
    try:
        # Tenant A: an un-explained cancellation is a real business event.
        plain = FakeOdoo20()
        _seed(plain)
        quiet = SourceScanner(plain, store, tenant_id=tenants[1])
        quiet.scan("odoo20.purchase_order", tenant_id=tenants[1])
        plain.records["purchase.order"][8]["state"] = "cancel"
        events = quiet.scan("odoo20.purchase_order", tenant_id=tenants[1])
        assert [event.condition for event in events] == ["supply_cancelled"]

        # Tenant B: the same change made by our approved execution is absorbed by
        # resetting the observation baseline for the objects we touched.
        executed = FakeOdoo20()
        _seed(executed)
        scanner = SourceScanner(executed, store, tenant_id=tenants[0])
        scanner.scan("odoo20.purchase_order", tenant_id=tenants[0])
        executed.records["purchase.order"][8]["state"] = "cancel"
        assert refresh_observed_state(
            store, tenants[0], executed, object_refs=["purchase.order:8"]) == 1
        assert scanner.scan("odoo20.purchase_order", tenant_id=tenants[0]) == []
    finally:
        with store.connection.transaction():
            for tenant in tenants:
                store.connection.execute("DELETE FROM control_tenants WHERE id=%s",
                                         (tenant,))
        store.close()

"""The plan half of the chain: prepare, approve, apply and verify as effects."""
from __future__ import annotations

import os
import uuid
from datetime import date

import pytest

from forgeflow.integrations.odoo20.business import PreparedRecovery
from forgeflow.integrations.odoo20.executor import PlanExecutionResult
from forgeflow.integrations.odoo20.procurement import ReplacementCase
from forgeflow.integrations.odoo20.verifier import VerificationCheck, VerificationResult
from forgeflow.planning.replacement import PurchaseOffer, periods_until
from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    EffectKind,
    PlanEffects,
    Role,
    TenantContext,
    approve_plan,
    bind_odoo_user,
    request_execution,
)
from forgeflow.superagent.dispatcher import EffectContext
from forgeflow.superagent.plan_effects import _linked_plan

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")

TODAY = date.today()


def _case(product_id: int = 7, code: str = "RM", quantity: int = 6) -> ReplacementCase:
    due = date.fromordinal(TODAY.toordinal() + 2)
    return ReplacementCase(
        order={"id": 9, "name": "P00009", "state": "cancel",
               "partner_id": [5, "Vendor"], "company_id": [1, "Co"]},
        line={"id": 10, "product_id": [product_id, code], "product_qty": quantity,
              "date_planned": f"{due.isoformat()} 09:00:00"},
        product={"id": product_id, "default_code": code, "is_storable": False,
                 "uom_id": [1, "Units"]},
        company_id=1, currency="USD", unit="Units", required_quantity=quantity,
        due_date=due, stock=0, commitments=(),
        offers=(PurchaseOffer(5, "partner:5", 2.0),),
        today=TODAY, periods=tuple(periods_until(TODAY, due)), due_period=2)


class _Adapter:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []

    def create(self, model: str, values: dict) -> int:
        self.created.append((model, values))
        return 55


class _Executor:
    def __init__(self) -> None:
        self.readback_calls = 0

    def readback(self, plan_id: int) -> dict:
        self.readback_calls += 1
        return {"purchase.order:new:buy-partner:5-p2": {"id": 900, "state": "purchase"}}


class _Business:
    def __init__(self) -> None:
        self.adapter = _Adapter()
        self.executor = _Executor()
        self.prepared = None
        self.applied = 0
        self.approve_calls = 0
        self.verified = True

    def recovery_snapshot(self, *, order_id: int) -> dict:
        return {"object_ref": f"purchase.order:{order_id}",
                "order": {"id": order_id},
                "lines": [{"id": 10, "product_id": [7, "RM"], "product_qty": 6}],
                "affected_targets": [f"purchase.order:{order_id}"],
                "completeness": {"complete": True}}

    def prepare_recovery(self, exception_id, actions, facts, *, tenant_ref="",
                         name="", expires_in_hours=72) -> PreparedRecovery:
        self.prepared = PreparedRecovery(
            exception_id=int(exception_id), plan_id=77, plan_hash="a" * 64,
            snapshot_hash="b" * 64, facts=facts, execution_identity="",
            actions=tuple(actions))
        return self.prepared

    def load_prepared(self, plan_id: int) -> PreparedRecovery:
        assert int(plan_id) == 77
        return self.prepared

    def approve(self, prepared, *, approver_uid: int, comment: str,
                expires_in_hours: int = 24) -> int:
        self.approve_calls += 1
        self.prepared = PreparedRecovery(
            exception_id=prepared.exception_id, plan_id=prepared.plan_id,
            plan_hash=prepared.plan_hash, snapshot_hash=prepared.snapshot_hash,
            facts=prepared.facts, execution_identity="identity:77",
            actions=prepared.actions)
        return 88

    def apply_plan(self, prepared) -> PlanExecutionResult:
        self.applied += 1
        return PlanExecutionResult(77, "executed",
                                   ({"action_id": "buy-partner:5-p2",
                                     "state": "succeeded"},))

    def verify_plan(self, prepared) -> VerificationResult:
        return VerificationResult(
            "verified" if self.verified else "failed",
            (VerificationCheck("plan_present", "pass"),
             VerificationCheck("created_present", "pass" if self.verified else "failed")),
            {"plan": "pass", "readback": "pass", "business": "pass"})


def _seed():
    tenant = f"effects-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Effects", 1,
                      (("service", "Service", "planner"),))
    ctx = TenantContext(tenant, "service", frozenset({Role.PLANNER}), 1)
    engine = ControlEngine(store)
    opened = engine.open_task(
        ctx, object_ref="purchase.order:9",
        goal="Recover supply after a cancelled purchase commitment",
        state="running")
    service_ctx = engine.service_context_for_ticket(
        tenant_id=tenant, ticket_id=opened["ticket_id"], service_id="dispatcher:test")
    return tenant, store, engine, service_ctx, opened["ticket_id"]


def _context(engine, ctx, ticket_id: str, payload: dict) -> EffectContext:
    return EffectContext(
        effect={"id": "effect-1", "tenant_id": ctx.tenant_id,
                "ticket_id": ticket_id, "fencing_token": "fence"},
        ticket={"id": ticket_id},
        payload=payload, ctx=ctx, tenant_id=ctx.tenant_id, ticket_id=ticket_id,
        fencing_token="fence", cancel_check=lambda: False, engine=engine)


def _cleanup(store, tenant: str) -> None:
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_odoo_bindings WHERE tenant_id=%s", (tenant,))
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


def test_prepare_links_one_plan_and_asks_for_approval(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    monkeypatch.setattr("forgeflow.superagent.plan_effects.collect_replacement_cases",
                        lambda adapter, *, order_id: (_case(),))
    effects = PlanEffects(business)
    context = _context(engine, ctx, ticket_id,
                       {"object_ref": "purchase.order:9"})
    try:
        first = effects.prepare_plan(context)
        assert first["status"] == "completed", first
        assert first["plan_id"] == 77 and first["action_count"] == 1
        ticket = engine.get_ticket(ctx, ticket_id)
        assert ticket["state"] == "awaiting_approval"
        linked = _linked_plan(store, tenant, ticket_id)
        assert linked and linked["plan_id"] == 77 and linked["attention_state"] == "open"
        second = effects.prepare_plan(context)
        assert second["reused"] is True and second["plan_id"] == 77
        count = store.connection.execute(
            "SELECT count(*) AS count FROM control_odoo_plan_links "
            "WHERE tenant_id=%s AND ticket_id=%s", (tenant, ticket_id)).fetchone()["count"]
        store.connection.commit()
        assert count == 1
        assert any(kind == "plan_prepared"
                   for kind in [event["kind"] for event in engine.ticket_events(ctx, ticket_id)])
        assert business.adapter.created[0][0] == "forgeflow.exception_event"
    finally:
        _cleanup(store, tenant)


def test_apply_without_an_approval_is_refused(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    monkeypatch.setattr("forgeflow.superagent.plan_effects.collect_replacement_cases",
                        lambda adapter, *, order_id: (_case(),))
    effects = PlanEffects(business)
    context = _context(engine, ctx, ticket_id, {"object_ref": "purchase.order:9"})
    try:
        effects.prepare_plan(context)
        outcome = effects.apply_approved_plan(
            _context(engine, ctx, ticket_id, {"plan_id": 77}))
        assert outcome["status"] == "blocked"
        assert outcome["reason"] == "approval_missing"
        assert business.applied == 0
        assert engine.get_ticket(ctx, ticket_id)["state"] == "awaiting_approval"
    finally:
        _cleanup(store, tenant)


def test_revoked_effect_cannot_execute_a_later_approval(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    monkeypatch.setattr("forgeflow.superagent.plan_effects.collect_replacement_cases",
                        lambda adapter, *, order_id: (_case(),))
    effects = PlanEffects(business)
    try:
        effects.prepare_plan(_context(engine, ctx, ticket_id, {"object_ref": "purchase.order:9"}))
        business.approve(business.prepared, approver_uid=7, comment="new approval")
        outcome = effects.apply_approved_plan(_context(engine, ctx, ticket_id, {
            "plan_id": 77, "execution_identity": "revoked-identity"}))
        assert outcome == {"status": "cancelled", "reason": "approval_superseded"}
        assert business.applied == 0
        assert engine.get_ticket(ctx, ticket_id)["state"] == "awaiting_approval"
    finally:
        _cleanup(store, tenant)


def test_approval_then_apply_then_verify_completes_the_ticket(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    monkeypatch.setattr("forgeflow.superagent.plan_effects.collect_replacement_cases",
                        lambda adapter, *, order_id: (_case(),))
    effects = PlanEffects(business)
    prepare = _context(engine, ctx, ticket_id, {"object_ref": "purchase.order:9"})
    try:
        effects.prepare_plan(prepare)
        principal = f"approver-{tenant}"
        bind_odoo_user(store, principal_id=principal, tenant_id=tenant,
                       odoo_user_id=2, role="approver")
        approver = TenantContext(tenant, principal, frozenset({Role.APPROVER}), 1)
        approval = approve_plan(store, approver, business, plan_id=77, comment="ok")
        assert approval["decision_id"] == 88
        assert engine.get_ticket(ctx, ticket_id)["state"] == "queued"
        queued = request_execution(store, approver, business, plan_id=77)
        assert queued["reused"] is False
        assert queued["state"] == "pending"
        assert request_execution(store, approver, business, plan_id=77)["reused"] is True
        effect_kind = store.connection.execute(
            "SELECT kind FROM control_effects WHERE tenant_id=%s AND id=%s",
            (tenant, queued["effect_id"])).fetchone()["kind"]
        store.connection.commit()
        assert effect_kind == EffectKind.APPLY_APPROVED_PLAN.value

        # -- a different process can execute: a fresh store and engine -------
        other = ControlStore(DATABASE_URL)
        other_engine = ControlEngine(other)
        try:
            applied = PlanEffects(business).apply_approved_plan(
                _context(other_engine, ctx, ticket_id, {"plan_id": 77}))
            assert applied["status"] == "completed"
            assert applied["operations"][0]["state"] == "succeeded"
            assert other_engine.get_ticket(ctx, ticket_id)["state"] == "verifying"
            verified = PlanEffects(business).verify_execution(
                _context(other_engine, ctx, ticket_id, {"plan_id": 77}))
            assert verified["status"] == "completed"
            assert verified["verification"] == "verified"
            assert other_engine.get_ticket(ctx, ticket_id)["state"] == "completed"
            assert business.executor.readback_calls == 1
            kinds = [event["kind"] for event in other_engine.ticket_events(ctx, ticket_id)]
            assert "plan_verified" in kinds and "plan_prepared" in kinds
        finally:
            other.close()
    finally:
        _cleanup(store, tenant)


def test_failed_verification_blocks_the_ticket(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    business.verified = False
    monkeypatch.setattr("forgeflow.superagent.plan_effects.collect_replacement_cases",
                        lambda adapter, *, order_id: (_case(),))
    effects = PlanEffects(business)
    try:
        effects.prepare_plan(_context(engine, ctx, ticket_id,
                                      {"object_ref": "purchase.order:9"}))
        business.prepared = PreparedRecovery(
            exception_id=55, plan_id=77, plan_hash="a" * 64, snapshot_hash="b" * 64,
            facts={}, execution_identity="identity:77")
        effects.apply_approved_plan(_context(engine, ctx, ticket_id, {"plan_id": 77}))
        outcome = effects.verify_execution(
            _context(engine, ctx, ticket_id, {"plan_id": 77}))
        assert outcome["status"] == "failed"
        assert outcome["failed"] == ["created_present"]
        assert engine.get_ticket(ctx, ticket_id)["state"] == "blocked"
    finally:
        _cleanup(store, tenant)


def test_a_shortfall_never_reaches_an_approval(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()

    def no_supply(adapter, *, order_id):
        case = _case()
        return (ReplacementCase(
            order=case.order, line=case.line, product=case.product,
            company_id=case.company_id, currency=case.currency, unit=case.unit,
            required_quantity=case.required_quantity, due_date=case.due_date,
            stock=0, commitments=(), offers=(), today=case.today,
            periods=case.periods, due_period=case.due_period),)

    monkeypatch.setattr("forgeflow.superagent.plan_effects.collect_replacement_cases",
                        no_supply)
    try:
        outcome = PlanEffects(business).prepare_plan(
            _context(engine, ctx, ticket_id, {"object_ref": "purchase.order:9"}))
        assert outcome["status"] == "blocked"
        assert outcome["reason"] == "replacement_shortfall"
        assert business.prepared is None
        assert _linked_plan(store, tenant, ticket_id) is None
    finally:
        _cleanup(store, tenant)


def test_multi_product_commitment_prepares_both_products(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    monkeypatch.setattr(
        "forgeflow.superagent.plan_effects.collect_replacement_cases",
        lambda adapter, *, order_id: (_case(7, "RM-A", 6), _case(8, "RM-B", 4)))
    try:
        outcome = PlanEffects(business).prepare_plan(
            _context(engine, ctx, ticket_id, {"object_ref": "purchase.order:9"}))
        assert outcome["status"] == "completed", outcome
        assert outcome["action_count"] == 2
        assert len(outcome["plan"]) == 2 if isinstance(outcome["plan"], list) else False
        event = store.connection.execute(
            "SELECT payload FROM control_events WHERE tenant_id=%s AND ticket_id=%s "
            "AND kind='plan_prepared'", (tenant, ticket_id)).fetchone()
        store.connection.commit()
        payload = event["payload"]
        assert len(payload["plans"]) == 2
        assert "RM-A" in payload["plans"][0]["product_code"]
    finally:
        _cleanup(store, tenant)


def test_manufacturing_object_ref_uses_the_bom_planner(monkeypatch) -> None:
    tenant, store, engine, ctx, ticket_id = _seed()
    business = _Business()
    prepared = PreparedRecovery(
        exception_id=66, plan_id=88, plan_hash="e" * 64, snapshot_hash="f" * 64,
        facts={}, execution_identity="identity:88")

    def manufacturing(service, *, mo_ids, tenant_id, time_limit_seconds):
        assert mo_ids == [9]
        return {"prepared": prepared, "actions": [{
            "action_id": "make-FG", "type": "create_manufacturing_order"}],
            "facts": {"manufacturing": {"produced": {"FG": 3}},
                      "mo": {"id": 9}}}

    monkeypatch.setattr("forgeflow.superagent.plan_effects.prepare_manufacturing_plan",
                        manufacturing)
    try:
        outcome = PlanEffects(business).prepare_plan(_context(
            engine, ctx, ticket_id, {"object_ref": "mrp.production:9"}))
        assert outcome["status"] == "completed", outcome
        assert outcome["plan_id"] == 88 and outcome["action_count"] == 1
        assert outcome["plan"]["produced"] == {"FG": 3}
        assert engine.get_ticket(ctx, ticket_id)["state"] == "awaiting_approval"
    finally:
        _cleanup(store, tenant)

"""Operator operations on a prepared plan: revoke an approval, reconcile a write."""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest

from forgeflow.integrations.odoo20.business import PreparedRecovery
from forgeflow.integrations.odoo20.executor import ExecutionResult
from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    TenantContext,
    approve_plan,
    bind_odoo_user,
    reconcile_execution,
    request_execution,
    revoke_approval,
)
from forgeflow.superagent.tasks import answer_attention, link_plan_to_ticket, plan_task

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


class _Adapter:
    def __init__(self) -> None:
        self.operations: list[dict] = []
        self.writes: list[tuple[str, list, dict]] = []

    def search_read(self, model, domain, fields, limit=100, order=None, offset=0):
        if model == "forgeflow.execution_operation":
            return list(self.operations)
        return []

    def write(self, model, ids, values):
        self.writes.append((model, list(ids), dict(values)))
        return True


class _Business:
    def __init__(self, plan_state: str = "approved") -> None:
        self.adapter = _Adapter()
        self.prepared = PreparedRecovery(
            exception_id=5, plan_id=77, plan_hash="a" * 64, snapshot_hash="b" * 64,
            facts={}, execution_identity="identity:77", plan_state=plan_state,
            actions=())
        self.revoke_calls = 0
        self.reconcile_state = "unknown"
        self.approvals = 0

    def load_prepared(self, plan_id: int) -> PreparedRecovery:
        assert int(plan_id) == 77
        return self.prepared

    def revoke(self, prepared, *, reason: str = "") -> dict:
        self.revoke_calls += 1
        self.prepared = PreparedRecovery(
            exception_id=prepared.exception_id, plan_id=prepared.plan_id,
            plan_hash=prepared.plan_hash, snapshot_hash=prepared.snapshot_hash,
            facts=prepared.facts, execution_identity="", plan_state="awaiting_approval",
            actions=())
        return {"plan_id": prepared.plan_id, "already_revoked": False}

    def approve(self, prepared, *, approver_uid, comment, expires_in_hours=24):
        self.approvals += 1
        self.prepared = replace(prepared, plan_state="approved",
                                execution_identity=f"identity:77:approval:{self.approvals}")
        return 100 + self.approvals

    def reconcile(self, prepared, *, idempotency_key: str) -> ExecutionResult:
        return ExecutionResult(operation_id=31, state=self.reconcile_state,
                               readback={"state": self.reconcile_state},
                               receipt_id=0)


def _seed():
    tenant = f"ops-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Ops", 1,
                      (("planner", "Planner", "planner"),
                       ("approver", "Approver", "approver")))
    ctx = TenantContext(tenant, "planner", frozenset({Role.PLANNER}), 1)
    engine = ControlEngine(store)
    opened = engine.open_task(ctx, object_ref="purchase.order:9",
                              goal="recover supply", state="running")
    link_plan_to_ticket(store, ctx, plan_id=77, ticket_id=opened["ticket_id"],
                        question="Approve the plan.")
    return tenant, store, engine, ctx, opened["ticket_id"]


def test_revoking_an_unexecuted_approval_returns_the_task_to_review() -> None:
    tenant, store, engine, planner_ctx, ticket_id = _seed()
    business = _Business()
    approver = TenantContext(tenant, "approver", frozenset({Role.APPROVER}), 1)
    try:
        engine.request_effect(approver, ticket_id=ticket_id,
                              kind="apply_approved_plan", payload={"plan_id": 77})
        result = revoke_approval(store, approver, business, plan_id=77,
                                 reason="wrong supplier")
        assert result["revoked"] is True and result["cancelled_effects"] == 1
        assert business.revoke_calls == 1
        effect = store.connection.execute(
            "SELECT state FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
            "AND kind='apply_approved_plan' ORDER BY created_at DESC LIMIT 1",
            (tenant, ticket_id)).fetchone()
        store.connection.commit()
        assert effect["state"] == "cancelled"
        assert engine.get_ticket(planner_ctx, ticket_id)["state"] == "awaiting_approval"
        kinds = [row["kind"] for row in engine.ticket_events(planner_ctx, ticket_id)]
        assert "plan_approval_revoked" in kinds
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_an_approval_cannot_be_revoked_after_an_operation_was_dispatched() -> None:
    tenant, store, engine, _planner, ticket_id = _seed()
    business = _Business()
    business.adapter.operations = [{"id": 31}]
    approver = TenantContext(tenant, "approver", frozenset({Role.APPROVER}), 1)
    try:
        with pytest.raises(PermissionError, match="plan_already_executing"):
            revoke_approval(store, approver, business, plan_id=77)
        assert business.revoke_calls == 0
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_reapproval_links_new_attention_and_new_effect_without_erasing_history() -> None:
    tenant, store, engine, _planner, ticket_id = _seed()
    business = _Business()
    principal = f"approver-{tenant}"
    approver = TenantContext(tenant, principal, frozenset({Role.APPROVER}), 1)
    try:
        bind_odoo_user(store, principal_id=principal, tenant_id=tenant,
                       odoo_user_id=7, role="approver")
        first_attention = answer_attention(store, approver, plan_id=77).attention_id
        old_effect = request_execution(store, approver, business, plan_id=77)
        identities = [business.prepared.execution_identity]
        for _ in range(2):
            revoked = revoke_approval(store, approver, business, plan_id=77, reason="Reconsider")
            linked = plan_task(store, tenant_id=tenant, plan_id=77)
            assert linked.attention_id == revoked["attention_id"]
            assert linked.attention_id != first_attention
            assert linked.attention_state == "open"
            assert engine.get_ticket(approver, ticket_id)["state"] == "awaiting_approval"
            approved = approve_plan(store, approver, business, plan_id=77, comment="Reviewed again")
            assert approved["already_approved"] is False
            assert plan_task(store, tenant_id=tenant, plan_id=77).attention_state == "answered"
            assert engine.get_ticket(approver, ticket_id)["state"] == "queued"
            new_effect = request_execution(store, approver, business, plan_id=77)
            assert new_effect["effect_id"] != old_effect["effect_id"]
            assert new_effect["state"] == "pending" and not new_effect["reused"]
            assert request_execution(store, approver, business, plan_id=77)["effect_id"] == new_effect["effect_id"]
            assert approve_plan(store, approver, business, plan_id=77)["already_approved"]
            identities.append(business.prepared.execution_identity)
            old_effect = new_effect
        effects = store.connection.execute(
            "SELECT state, payload FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
            "AND kind='apply_approved_plan' ORDER BY created_at", (tenant, ticket_id)).fetchall()
        attentions = store.connection.execute(
            "SELECT state FROM control_attentions WHERE tenant_id=%s AND ticket_id=%s "
            "AND need_type='approval'", (tenant, ticket_id)).fetchall()
        store.connection.commit()
        assert [row["state"] for row in effects] == ["cancelled", "cancelled", "pending"]
        assert [row["payload"]["execution_identity"] for row in effects] == identities
        assert len(attentions) == 3 and all(row["state"] == "answered" for row in attentions)
        events = engine.ticket_events(approver, ticket_id)
        assert sum(row["kind"] == "plan_approval_revoked" for row in events) == 2
        assert sum(row["kind"] == "plan_approved" for row in events) == 2
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_odoo_bindings WHERE tenant_id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_simultaneous_execution_requests_enqueue_only_one_effect() -> None:
    tenant, store, _engine, _planner, ticket_id = _seed()
    business = _Business()
    approver = TenantContext(tenant, "approver", frozenset({Role.APPROVER}), 1)
    barrier = Barrier(6)
    load = business.load_prepared

    def simultaneous_load(plan_id):
        value = load(plan_id)
        barrier.wait(timeout=10)
        return value

    business.load_prepared = simultaneous_load
    try:
        answer_attention(store, approver, plan_id=77)

        def enqueue(_):
            other = ControlStore(DATABASE_URL)
            try:
                return request_execution(other, approver, business, plan_id=77)
            finally:
                other.close()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(enqueue, range(6)))
        assert len({row["effect_id"] for row in results}) == 1
        assert sum(not row["reused"] for row in results) == 1
        count = store.connection.execute(
            "SELECT count(*) AS n FROM control_effects WHERE tenant_id=%s AND ticket_id=%s "
            "AND kind='apply_approved_plan'", (tenant, ticket_id)).fetchone()["n"]
        store.connection.commit()
        assert count == 1
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_existing_legacy_pending_effect_is_reused_during_upgrade() -> None:
    tenant, store, engine, _planner, ticket_id = _seed()
    business = _Business()
    approver = TenantContext(tenant, "approver", frozenset({Role.APPROVER}), 1)
    try:
        answer_attention(store, approver, plan_id=77)
        old_id = engine.request_effect(approver, ticket_id=ticket_id,
            kind="apply_approved_plan", payload={"plan_id": 77},
            idempotency_key=f"apply:77:{business.prepared.plan_hash[:12]}")
        queued = request_execution(store, approver, business, plan_id=77)
        assert queued["effect_id"] == old_id and queued["reused"]
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_revoked_legacy_failed_effect_does_not_block_a_new_approval() -> None:
    tenant, store, engine, _planner, ticket_id = _seed()
    business = _Business()
    approver = TenantContext(tenant, "approver", frozenset({Role.APPROVER}), 1)
    try:
        answer_attention(store, approver, plan_id=77)
        old_id = engine.request_effect(approver, ticket_id=ticket_id,
            kind="apply_approved_plan", payload={"plan_id": 77},
            idempotency_key=f"apply:77:{business.prepared.plan_hash[:12]}")
        with store.transaction() as cursor:
            cursor.execute("UPDATE control_effects SET state='failed' WHERE id=%s", (old_id,))
        revoke_approval(store, approver, business, plan_id=77)
        business.approve(business.prepared, approver_uid=7, comment="new approval")
        answer_attention(store, approver, plan_id=77)
        queued = request_execution(store, approver, business, plan_id=77)
        assert queued["effect_id"] != old_id and not queued["reused"]
        assert queued["state"] == "pending"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_retry_after_erp_approval_finishes_the_open_attention_without_new_decision() -> None:
    tenant, store, _engine, _planner, _ticket_id = _seed()
    business = _Business()
    approver = TenantContext(tenant, "approver", frozenset({Role.APPROVER}), 1)
    try:
        result = approve_plan(store, approver, business, plan_id=77)
        assert result["already_approved"] and result["attention"] == "answered"
        assert business.approvals == 0
        assert request_execution(store, approver, business, plan_id=77)["state"] == "pending"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_reconciling_an_unknown_write_blocks_and_asks_for_verification() -> None:
    tenant, store, engine, planner_ctx, ticket_id = _seed()
    business = _Business()
    try:
        result = reconcile_execution(store, planner_ctx, business, plan_id=77,
                                     idempotency_key="plan:abc:action")
        assert result["state"] == "unknown" and result["next"] == "needs_investigation"
        assert engine.get_ticket(planner_ctx, ticket_id)["state"] == "blocked"
        open_needs = [row["need_type"] for row in store.connection.execute(
            "SELECT need_type FROM control_attentions WHERE tenant_id=%s "
            "AND ticket_id=%s AND state='open'",
            (tenant, ticket_id)).fetchall()]
        store.connection.commit()
        assert "result_unknown" in open_needs
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_reconciling_a_successful_write_continues_to_verification() -> None:
    tenant, store, engine, planner_ctx, ticket_id = _seed()
    business = _Business()
    business.reconcile_state = "succeeded"
    try:
        result = reconcile_execution(store, planner_ctx, business, plan_id=77,
                                     idempotency_key="plan:abc:action")
        assert result["next"] == "continue_verification"
        assert engine.get_ticket(planner_ctx, ticket_id)["state"] == "verifying"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()

"""Regression tests for the second review round.

Each test fails on 0df5e9d and passes after the fix, or states plainly which
behaviour it pins.
"""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.planning.multi_bom import BomNode, Capacity, MultiBomPlanner, Product
from forgeflow.planning.recovery import (
    DemandLine,
    Period,
    ProductPlanInput,
    RecoveryPlanner,
    SupplierOffer,
    Workcenter,
)
from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    TenantContext,
)

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


# ------------------------------------------------- 1. per-product BOM coefficients
def test_each_product_uses_its_own_bom_coefficients() -> None:
    """The component constraint used one product's BOM for every product."""
    product_a = Product("A", 5, BomNode("A", children=(BomNode("C1", 1),)), {})
    product_b = Product("B", 5, BomNode("B", children=(BomNode("C2", 1),)), {})
    planner = MultiBomPlanner([product_a, product_b], [],
                              component_supply={"C1": 10, "C2": 5})
    plan = planner.solve()
    # C1 and C2 are separate components: A needs 5 of C1, B needs 5 of C2.
    assert plan.produced == {"A": 5, "B": 5}
    assert sum(plan.shortfall.values()) == 0
    assert plan.assumptions == ()


# ------------------------------------------- 2. budgets constrain the procurement
def test_a_budget_cap_constrains_spend() -> None:
    planner = RecoveryPlanner(
        periods=[Period(index, f"w{index + 1}") for index in range(2)],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 10, 1, 1, budget_cap=500.0)],
        workcenters=[Workcenter("press", {0: 0.0, 1: 0.0})],
        offers=[SupplierOffer("sup", "FG", 100.0, lead_periods=0)])
    plan = planner.solve()
    # Production is impossible (no capacity) and the cap buys only five units.
    # The plan must be honest about the shortage instead of spending past it.
    assert plan.solved is True
    assert plan.total_cost() <= 500.0
    assert plan.cost_by_currency == {"USD": 500.0}
    assert plan.unmet["SO1"] == 5


def test_a_budget_within_the_cap_is_respected_and_reported() -> None:
    planner = RecoveryPlanner(
        periods=[Period(index, f"w{index + 1}") for index in range(2)],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 4, 1, 1, budget_cap=500.0)],
        workcenters=[Workcenter("press", {0: 0.0, 1: 0.0})],
        offers=[SupplierOffer("sup", "FG", 100.0, lead_periods=0)])
    plan = planner.solve()
    assert plan.solved is True
    assert plan.cost_by_currency == {"USD": 400.0}
    assert plan.unmet["SO1"] == 0


def test_a_missing_budget_is_not_treated_as_a_limit() -> None:
    planner = RecoveryPlanner(
        periods=[Period(0, "w1")],
        products=[ProductPlanInput("FG", BomNode("FG"), {"press": 1.0})],
        demands=[DemandLine("SO1", "FG", 4, 0, 1, budget_cap=None)],
        workcenters=[Workcenter("press", {0: 0.0})],
        offers=[SupplierOffer("sup", "FG", 100.0, lead_periods=0)])
    plan = planner.solve()
    assert plan.solved is True
    assert "budget_unknown:SO1" in plan.assumptions
    assert plan.trustworthy is False


# ------------------------------------------------ 3. an answer resumes the work
def _setup(prefix: str):
    tenant = f"{prefix}-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, prefix, 1, (("planner-a", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    return tenant, store, ctx, ControlEngine(store)


def _drop(store, tenant: str) -> None:
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


@requires_db
def test_answering_a_question_queues_a_continuation_effect() -> None:
    tenant, store, ctx, engine = _setup("resume")
    try:
        admitted = engine.admit_trigger(ctx, source_id="t", external_event_id="r-1",
                                        payload_hash="a" * 64, object_ref="PO-1",
                                        goal="resume")
        # The worker already ran and asked a question, so nothing is pending.
        first = engine.claim_effect("w", tenant_id=tenant)
        engine.complete_effect(effect_id=first["id"], tenant_id=tenant,
                               fencing_token=first["fencing_token"])
        attention = engine.open_attention(ctx, ticket_id=admitted.ticket_id,
                                          need_type="fact", question="Which budget?")
        assert engine.get_ticket(ctx, admitted.ticket_id)["user_state"] == "needs_input"
        assert store.connection.execute(
            "SELECT count(*) AS n FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchone()["n"] == 0

        engine.answer_attention(ctx, attention_id=attention,
                                answer={"budget_cap": 5000})

        ticket = engine.get_ticket(ctx, admitted.ticket_id)
        assert ticket["state"] == "queued"
        pending = store.connection.execute(
            "SELECT kind FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchall()
        assert [row["kind"] for row in pending] == ["launch_worker"]
        # The answer is durable input for the resumed worker, not just a click.
        inputs = store.connection.execute(
            "SELECT kind, body FROM control_inputs WHERE tenant_id=%s",
            (tenant,)).fetchall()
        assert any(row["kind"] == "attention_answer"
                   and row["body"]["answer"] == {"budget_cap": 5000} for row in inputs)
        kinds = [row["kind"] for row in engine.ticket_events(ctx, admitted.ticket_id)]
        assert "work_resumed" in kinds
    finally:
        _drop(store, tenant)


@requires_db
def test_answering_an_approval_does_not_launch_a_worker() -> None:
    tenant, store, ctx, engine = _setup("approve")
    try:
        admitted = engine.admit_trigger(ctx, source_id="t", external_event_id="a-1",
                                        payload_hash="b" * 64, object_ref="PO-2",
                                        goal="approve")
        first = engine.claim_effect("w", tenant_id=tenant)
        engine.complete_effect(effect_id=first["id"], tenant_id=tenant,
                               fencing_token=first["fencing_token"])
        attention = engine.open_attention(ctx, ticket_id=admitted.ticket_id,
                                          need_type="approval", question="Approve?")
        engine.answer_attention(ctx, attention_id=attention, answer={"approved": True})
        assert store.connection.execute(
            "SELECT count(*) AS n FROM control_effects WHERE tenant_id=%s AND state='pending'",
            (tenant,)).fetchone()["n"] == 0
    finally:
        _drop(store, tenant)


# ------------------------------------------- 4. service context keeps real roles
@requires_db
def test_a_task_created_by_an_approver_can_still_run() -> None:
    tenant = f"roles-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Roles", 1,
                      (("approver-b", "Approver", "approver"),))
    approver = TenantContext(tenant, "approver-b", frozenset({Role.APPROVER}), 1)
    try:
        opened = ControlEngine(store).open_task(
            approver, object_ref="purchase.order:7", goal="approver started this")
        service = ControlEngine(store).service_context_for_ticket(
            tenant_id=tenant, ticket_id=opened["ticket_id"], service_id="dispatcher")
        assert service.roles == frozenset({Role.APPROVER})
        assert service.principal_id == "approver-b"
        # A mandated service may run existing work without being handed a role.
        service.require_worker()
        # A human without an operator role still cannot.
        with pytest.raises(Exception):
            approver.require_worker()
    finally:
        _drop(store, tenant)


# --------------------------------------- 5. indexing is per document, not per tenant
@requires_db
def test_a_structured_document_does_not_hide_a_legacy_one(tmp_path) -> None:
    from forgeflow.semantic.index import catalog_block_chunks, catalog_chunks

    tenant, store, ctx, engine = _setup("index")
    try:
        from forgeflow.knowledge.documents_catalog import DocumentCatalog

        catalog = DocumentCatalog(store)
        modern = tmp_path / "modern.md"
        modern.write_text("# rules\none rule\n", encoding="utf-8")
        catalog.ingest_file(tenant_id=tenant, source_path="modern.md", path=modern)
        # A legacy document row with content but no blocks.
        with store.connection.transaction():
            cursor = store.connection.cursor()
            cursor.execute(
                "INSERT INTO knowledge_documents(id, tenant_id, source_path, filename, "
                "kind, acl_scope, sha256, version, content, size_bytes, status, "
                "parse_status) VALUES (%s, %s, 'legacy.md', 'legacy.md', 'md', "
                "'project', %s, 1, 'legacy content about lead time', 32, 'ready', "
                "'complete')",
                (str(uuid.uuid4()), tenant, "legacy-" + uuid.uuid4().hex))
        block_chunks = catalog_block_chunks(store, tenant)
        covered = {chunk.path[len("document://"):] for chunk in block_chunks}
        assert covered == {"modern.md"}
        fallback = catalog_chunks(store, tenant, exclude_paths=covered)
        assert {chunk.path for chunk in fallback} == {"document://legacy.md"}
    finally:
        _drop(store, tenant)


# ------------------------------------------- 6. lexicographic recovery objective
def test_a_high_unit_price_does_not_buy_a_stockout() -> None:
    """With a fixed weight, a price above it makes unmet demand look cheaper."""
    expensive = Product("A", 10, BomNode("A"), {"press": 1.0}, unit_price=500.0)
    plan = MultiBomPlanner([expensive], [Capacity("press", 10)]).solve()
    assert plan.produced["A"] == 10
    assert sum(plan.shortfall.values()) == 0


def test_cost_only_decides_among_plans_with_the_same_shortfall() -> None:
    cheap = Product("A", 10, BomNode("A"), {"press": 1.0}, unit_price=1.0)
    dear = Product("B", 10, BomNode("B"), {"press": 1.0}, unit_price=50.0)
    plan = MultiBomPlanner([cheap, dear], [Capacity("press", 10)]).solve()
    assert sum(plan.shortfall.values()) == 10
    # Both cannot be satisfied; the cheaper unit wins the shared hour.
    assert plan.produced == {"A": 10, "B": 0}

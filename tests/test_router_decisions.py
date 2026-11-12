"""Router decisions: schema, heuristics, model policy and engine validation."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    HeuristicRouterPolicy,
    ModelReply,
    ModelRouterPolicy,
    Role,
    Router,
    RouterDecision,
    RouterDirective,
    RouterRequest,
    TenantContext,
    WorkerError,
)


class _Resolver:
    """Minimal resolver: the Router must ask the ERP, not guess an id."""

    def resolve(self, reference: str):
        from forgeflow.integrations.odoo20.resolver import Candidate, Resolution
        if reference.upper().startswith("PO-"):
            candidate = Candidate("purchase.order", 7, reference, reference, 1, "name")
            return Resolution(reference=reference, model_hint="purchase.order",
                              candidates=[candidate])
        return Resolution(reference=reference, model_hint=None, candidates=[])


def _request(message, **kwargs):
    defaults = dict(tenant_id="tenant-a", principal_id="planner-a", message=message)
    defaults.update(kwargs)
    return RouterRequest(**defaults)


def test_stop_wait_ignore_and_acknowledgement() -> None:
    policy = HeuristicRouterPolicy()
    assert policy.decide(_request("stop investigating PO-7")).decision == RouterDecision.STOP_TASK
    assert policy.decide(_request("wait for now")).decision == RouterDecision.WAIT_FOR_EVENT
    assert policy.decide(_request("thanks")).decision == RouterDecision.ANSWER
    assert policy.decide(_request("   ")).decision == RouterDecision.IGNORE


def test_no_identifiable_object_asks_a_question() -> None:
    directive = HeuristicRouterPolicy().decide(_request("what happened to my order?"))
    assert directive.decision == RouterDecision.REQUEST_CLARIFICATION
    assert directive.question


def test_object_is_extracted_from_common_business_references() -> None:
    policy = HeuristicRouterPolicy()
    # The router reports the reference the user wrote; it never turns the digits
    # into a record id (see tests/test_business_object_resolution.py).
    assert policy.decide(_request("PO-12 is late")).object_reference == "PO-12"
    assert policy.decide(_request("MO-9 capacity conflict")).object_reference == "MO-9"
    late = policy.decide(_request("PO-12 is late"))
    assert late.object_ref is None


def test_odoo_default_purchase_number_is_not_rewritten() -> None:
    """P00046 is the name Odoo stores; PO46 is a different number.

    The previous pattern matched ``P00046`` as prefix "P" + digits, then
    answered ``PO46``.  An exact lookup then finds no order, or finds another
    one whose name happens to be PO46.  The reference must survive verbatim and
    the model must travel as a separate hint.
    """
    directive = HeuristicRouterPolicy().decide(_request("P00046 交期延后，供应商取消"))
    assert directive.object_reference == "purchase.order:P00046"
    assert directive.decision == RouterDecision.START_INVESTIGATION


def test_spoken_reference_keeps_its_digits_and_names_the_model() -> None:
    """“采购单 46” is not silently renumbered to a zero-padded guess."""
    directive = HeuristicRouterPolicy().decide(_request("采购单 46 延期了"))
    assert directive.object_reference == "purchase.order:46"
    assert directive.decision == RouterDecision.START_INVESTIGATION


def test_existing_task_for_the_same_object_is_preferred() -> None:
    # A name cannot be matched against a record: "PO-7" must be resolved before
    # the router can know it is the order already under investigation.
    unresolved = HeuristicRouterPolicy().decide(_request(
        "PO-7 supplier moved the date",
        active_tickets=({"ticket_id": "ticket-1", "object_ref": "purchase.order:7"},)))
    assert unresolved.decision == RouterDecision.START_INVESTIGATION
    assert unresolved.object_reference == "PO-7"
    assert unresolved.object_ref is None

    resolved = HeuristicRouterPolicy().decide(_request(
        "supplier moved the date", object_ref="purchase.order:7",
        active_tickets=({"ticket_id": "ticket-1", "object_ref": "purchase.order:7"},)))
    assert resolved.decision == RouterDecision.ATTACH_TO_EXISTING_TASK
    assert resolved.ticket_id == "ticket-1"


def test_plan_language_requests_planning() -> None:
    directive = HeuristicRouterPolicy().decide(
        _request("PO-7 cancelled, give me a recovery plan"))
    assert directive.decision == RouterDecision.PREPARE_PLAN
    assert directive.capability == "plan"


def test_directive_schema_is_enforced() -> None:
    with pytest.raises(WorkerError, match="router_decision_invalid"):
        RouterDirective("do_something_unlisted")
    with pytest.raises(WorkerError, match="clarification_question_required"):
        RouterDirective(RouterDecision.REQUEST_CLARIFICATION.value)
    with pytest.raises(WorkerError, match="router_goal_required"):
        RouterDirective(RouterDecision.START_INVESTIGATION.value)


class _Runner:
    def __init__(self, replies):
        self.replies = list(replies)

    def complete(self, prompt, *, run_token=None):
        del run_token
        return ModelReply(self.replies.pop(0), {"tokens": 5, "complete": True}, True)


def test_model_policy_parses_a_schema_constrained_decision() -> None:
    runner = _Runner([json.dumps({
        "decision": "start_investigation", "rationale": "supplier cancelled",
        "object_ref": "purchase.order:7", "goal": "recover supply",
        "capability": "assess", "confidence": 0.9})])
    directive = ModelRouterPolicy(runner).decide(_request("PO-7 was cancelled"))
    assert directive.decision == RouterDecision.START_INVESTIGATION
    assert directive.object_ref == "purchase.order:7"
    assert directive.confidence == 0.9


def test_model_policy_falls_back_to_heuristics_on_bad_output() -> None:
    runner = _Runner(["not json"])
    directive = ModelRouterPolicy(runner).decide(_request("PO-7 was cancelled"))
    assert directive.decision == RouterDecision.START_INVESTIGATION


# ---------------------------------------------------------------- engine checks
DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


@pytest.fixture
def tenant_context():
    tenant = f"router-{uuid.uuid4().hex}"
    store = ControlStore(DATABASE_URL)
    store.migrate()
    store.seed_tenant(tenant, "Router", 1, (("planner-a", "Planner", "planner"),))
    ctx = TenantContext(tenant, "planner-a", frozenset({Role.PLANNER}), 1)
    yield store, ctx, tenant
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
    store.close()


@requires_db
def test_router_proposal_becomes_an_engine_command(tenant_context) -> None:
    store, ctx, _tenant = tenant_context
    engine = ControlEngine(store)
    router = Router(store)
    outcome = router.apply(ctx, engine, _request("PO-7 supplier cancelled, recover supply"), resolver=_Resolver())
    assert outcome["accepted"] is True and outcome["ticket_id"]
    ticket = engine.get_ticket(ctx, outcome["ticket_id"])
    assert ticket["user_state"] == "investigating"
    mandate = engine.get_mandate(ctx, ticket["mandate_id"])
    assert mandate.object_refs == frozenset({"purchase.order:7"})


@requires_db
def test_router_attach_adds_context_without_widening_authority(tenant_context) -> None:
    store, ctx, _tenant = tenant_context
    engine = ControlEngine(store)
    router = Router(store)
    first = router.apply(ctx, engine, _request("PO-7 supplier cancelled"), resolver=_Resolver())
    ticket_id = first["ticket_id"]
    before = engine.get_ticket(ctx, ticket_id)["mandate_id"]
    attach = router.apply(ctx, engine, _request(
        "PO-7 also affects SO-3", active_tickets=(
            {"ticket_id": ticket_id, "object_ref": "purchase.order:7"},)),
        resolver=_Resolver())
    assert attach["accepted"] is True and attach["ticket_id"] == ticket_id
    assert engine.get_ticket(ctx, ticket_id)["mandate_id"] == before
    inputs = store.connection.execute(
        "SELECT count(*) AS count FROM control_inputs WHERE tenant_id=%s AND ticket_id=%s",
        (ctx.tenant_id, ticket_id)).fetchone()["count"]
    assert int(inputs) == 1


@requires_db
def test_router_stop_cancels_through_the_engine(tenant_context) -> None:
    store, ctx, _tenant = tenant_context
    engine = ControlEngine(store)
    router = Router(store)
    started = router.apply(ctx, engine, _request("PO-7 supplier cancelled"), resolver=_Resolver())
    ticket_id = started["ticket_id"]
    stopped = router.apply(ctx, engine, _request("stop PO-7", ticket_id=ticket_id), resolver=_Resolver())
    assert stopped["accepted"] is True
    assert engine.get_ticket(ctx, ticket_id)["state"] == "cancelled"


@requires_db
def test_router_clarification_opens_one_attention(tenant_context) -> None:
    store, ctx, _tenant = tenant_context
    engine = ControlEngine(store)
    router = Router(store)
    started = router.apply(ctx, engine, _request("PO-7 supplier cancelled"), resolver=_Resolver())
    ticket_id = started["ticket_id"]
    outcome = router.apply(ctx, engine, _request(
        "something is wrong", ticket_id=ticket_id))
    assert outcome["accepted"] is True and outcome.get("attention_id")
    assert [row["need_type"] for row in engine.list_attentions(ctx)] == ["clarification"]


def test_router_uses_the_injected_model_policy_and_degrades_without_one() -> None:
    from forgeflow.superagent.model_runners import HttpModelRunner

    # A deployment without model configuration keeps the deterministic policy.
    assert HttpModelRunner.from_env({}) is None
    configured = HttpModelRunner.from_env({
        "FORGEFLOW_MODEL_BASE_URL": "https://model.example/v1",
        "FORGEFLOW_MODEL_API_KEY": "not-a-real-key",
        "FORGEFLOW_MODEL": "test-model"})
    assert configured is not None and configured.model == "test-model"

    class Policy:
        def decide(self, request):
            return RouterDirective(RouterDecision.ANSWER.value, "policy_used",
                                   confidence=0.9)

    assert Router(policy=Policy()).decide(_request("hello")).rationale == "policy_used"
    assert Router().decide(_request("hello")).rationale != "policy_used"

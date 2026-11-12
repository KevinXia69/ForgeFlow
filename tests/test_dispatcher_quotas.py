"""Dispatcher quota gate: block before launch, charge after a worker result."""
from __future__ import annotations

from types import SimpleNamespace

from forgeflow.superagent.dispatcher import Dispatcher


class _Quotas:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.consumed = []

    def reserve_many(self, tenant, items):
        self.consumed.extend((tenant, kind, amount) for kind, amount in items)
        return {"allowed": self.allowed,
                "items": [{"allowed": self.allowed, "kind": kind, "amount": amount,
                           "denied_scope": None if self.allowed else "tenant"}
                          for kind, amount in items],
                "window_start": "2026-11-08",
                "denied_scope": None if self.allowed else "tenant"}

    def consume(self, tenant, kind, amount, **kwargs):
        self.consumed.append((tenant, kind, amount))
        return {"allowed": True}

    def settle(self, tenant, kind, reserved, actual, **kwargs):
        self.consumed.append((tenant, kind, actual))
        return {"allowed": True}


class _Engine:
    def __init__(self):
        self.attentions = []

    def open_attention(self, ctx, **kwargs):
        self.attentions.append(kwargs)
        return "attention-1"


def _dispatcher(quotas):
    dispatcher = Dispatcher.__new__(Dispatcher)
    dispatcher.quotas = quotas
    dispatcher.engine = _Engine()
    return dispatcher


def test_exhausted_quota_opens_a_budget_attention_and_starts_no_worker() -> None:
    dispatcher = _dispatcher(_Quotas(allowed=False))
    context = SimpleNamespace(ctx=object(), tenant_id="tenant-a", ticket_id="ticket-1")
    request = SimpleNamespace(budget_seconds=10, budget_tokens=100)
    blocked = dispatcher._quota_block(context, request)
    assert blocked["status"] == "blocked"
    assert blocked["reason"] == "quota_exhausted"
    assert dispatcher.engine.attentions[0]["need_type"] == "budget"
    assert dispatcher.engine.attentions[0]["ticket_id"] == "ticket-1"


def test_allowed_quota_does_not_block_and_usage_is_charged() -> None:
    quotas = _Quotas(allowed=True)
    dispatcher = _dispatcher(quotas)
    context = SimpleNamespace(ctx=object(), tenant_id="tenant-a", ticket_id="ticket-1")
    request = SimpleNamespace(budget_seconds=10, budget_tokens=100)
    assert dispatcher._quota_block(context, request) is None
    result = SimpleNamespace(usage={"tokens": 250}, usage_complete=True)
    dispatcher._consume_usage(context, request, result, elapsed=1.2)
    assert ("tenant-a", "model_tokens", 250) in quotas.consumed
    assert ("tenant-a", "wall_seconds", 2) in quotas.consumed


def test_a_blocked_quota_does_not_consume_inputs_or_start_an_assignment() -> None:
    dispatcher = _dispatcher(_Quotas(allowed=False))
    called = []

    def must_not_run(*args, **kwargs):
        called.append("ran")
        raise AssertionError("blocked launch must not consume inputs or start work")

    dispatcher.engine.consume_inputs = must_not_run
    dispatcher.engine.start_assignment = must_not_run
    context = SimpleNamespace(
        payload={}, ticket={"goal": "investigate", "mandate_id": None},
        ctx=SimpleNamespace(principal_id="planner-a"),
        tenant_id="tenant-a", ticket_id="ticket-1", fencing_token="fence-1")
    result = dispatcher._handle_launch_worker(context)
    assert result["status"] == "blocked"
    assert called == []


def test_a_failed_launch_settles_wall_time_but_keeps_token_reservation() -> None:
    quotas = _Quotas(allowed=True)
    dispatcher = _dispatcher(quotas)
    context = SimpleNamespace(tenant_id="tenant-a", ticket_id="ticket-1")
    request = SimpleNamespace(budget_seconds=10, budget_tokens=100)
    dispatcher._settle_failed(context, request, elapsed=2.2,
                              window_start="2026-11-08")
    assert ("tenant-a", "wall_seconds", 3) in quotas.consumed
    assert not any(kind == "model_tokens" for _, kind, _ in quotas.consumed)

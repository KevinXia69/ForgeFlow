from __future__ import annotations

import pytest

from forgeflow.superagent import (
    CallableWorker,
    DshWorker,
    PiWorker,
    Role,
    Router,
    TenantContext,
    WorkerError,
    WorkerRequest,
    WorkerResult,
)


def _ctx() -> TenantContext:
    return TenantContext("tenant-a", "planner-a", frozenset({Role.PLANNER}), 1)


def _request(capability: str = "assess") -> WorkerRequest:
    return WorkerRequest("tenant-a", "ticket-a", "assess", {}, capability,
                         budget_seconds=10, budget_tokens=100)


def test_router_dispatches_by_capability() -> None:
    seen = []

    def run(request):
        seen.append(request.capability)
        return WorkerResult("completed", "done", usage={"tokens": 10})

    router = Router()
    router.register(CallableWorker("dsh", {"assess"}, run))
    result = router.dispatch(_ctx(), _request())
    assert result.status == "completed" and seen == ["assess"]


def test_router_rejects_unknown_capability_and_duplicate_registration() -> None:
    router = Router()
    router.register(CallableWorker("dsh", {"assess"}, lambda request: WorkerResult("completed", "ok")))
    with pytest.raises(WorkerError, match="capability_already_registered"):
        router.register(CallableWorker("pi", {"assess"}, lambda request: WorkerResult("completed", "ok")))
    with pytest.raises(WorkerError, match="capability_unavailable"):
        router.dispatch(_ctx(), _request("unknown"))


def test_router_rejects_invalid_budget_and_overrun() -> None:
    router = Router()
    router.register(CallableWorker("dsh", {"assess"}, lambda request: WorkerResult(
        "completed", "ok", usage={"tokens": 101})))
    with pytest.raises(WorkerError, match="token_budget"):
        router.dispatch(_ctx(), _request())
    with pytest.raises(WorkerError, match="budget_invalid"):
        router.dispatch(_ctx(), WorkerRequest("tenant-a", "ticket-a", "x", {}, "assess", 0, 100))


def test_dsh_worker_parses_bounded_structured_result() -> None:
    worker = DshWorker("dsh", {"assess"}, lambda prompt: (
        '{"status":"completed","summary":"found two affected orders",'
        '"findings":[{"order":"SO1"}],"evidence":[{"path":"docs/a.md"}],'
        '"unknowns":[],"attention":[]}'))
    result = worker.run(_request())
    assert result.status == "completed" and result.findings[0]["order"] == "SO1"


def test_pi_worker_fails_closed_on_invalid_output() -> None:
    worker = PiWorker("pi", {"research"}, lambda prompt: "not json")
    result = worker.run(WorkerRequest("tenant-a", "ticket-a", "research", {}, "research"))
    assert result.status == "failed" and result.summary == "worker_output_invalid"

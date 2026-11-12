"""The tool loop: real tool calls, budget, cancellation and structured reports."""
from __future__ import annotations

import json

import pytest

from forgeflow.superagent import (
    InvestigationToolBox,
    ModelReply,
    ToolLoopWorker,
    WorkerCancelled,
    WorkerError,
    WorkerRequest,
)
from forgeflow.superagent.identity import ObjectScope
from forgeflow.superagent.workers import _scope_for


class ScriptedRunner:
    """Deterministic model double that records every prompt it was given."""

    def __init__(self, replies, *, tokens=10, reports_usage=True):
        self.replies = list(replies)
        self.prompts = []
        self.tokens = tokens
        self.reports_usage = reports_usage

    def complete(self, prompt, *, run_token=None):
        self.prompts.append(prompt)
        text = self.replies.pop(0) if self.replies else '{"action":"final","report":{}}'
        return ModelReply(text, {"tokens": self.tokens, "complete": self.reports_usage},
                          self.reports_usage)


def _request(**kwargs):
    defaults = dict(tenant_id="tenant-a", ticket_id="ticket-1", goal="investigate PO-7",
                    input={"object_ref": "purchase.order:7"}, capability="assess",
                    budget_seconds=60, budget_tokens=1000, mandate_id="mandate-1",
                    run_token="run-1", object_ref="purchase.order:7",
                    tools=("investigation_query",))
    defaults.update(kwargs)
    return WorkerRequest(**defaults)


def _box(*, observed=True):
    def dispatch(action, payload):
        if observed:
            return {"records": [{"entity": "purchase_order", "id": 7,
                                 "fields": {"state": "cancel"},
                                 "observation_id": "obs-7"}],
                    "complete": True, "next_cursor": None}
        return {"status": "tool_error", "code": "invalid_arguments"}

    return InvestigationToolBox(dispatch)


def test_loop_calls_a_tool_then_returns_a_structured_report() -> None:
    runner = ScriptedRunner([
        json.dumps({"action": "tool", "tool": "investigation_query",
                    "arguments": {"kind": "record",
                                  "arguments": {"entity": "purchase_order", "id": 7}},
                    "why": "confirm the cancellation"}),
        json.dumps({"action": "final", "report": {
            "status": "completed", "summary": "supplier cancelled PO-7",
            "findings": [{"kind": "observation", "statement": "PO-7 is cancelled",
                          "source": "investigation_query"}],
            "evidence": [{"source": "investigation_query", "record": "PO-7",
                          "field": "state"}],
            "unknowns": [], "attention": [],
            "plan_request": {"objective": "replace supply",
                             "snapshot_ref": "snap-1", "facts": {"po": 7}}}}),
    ])
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box())
    result = worker.run(_request())
    assert result.status == "completed"
    assert result.summary == "supplier cancelled PO-7"
    assert result.findings[0]["statement"] == "PO-7 is cancelled"
    assert result.plan_request["snapshot_ref"] == "snap-1"
    # The loop really ran two model turns, and the second prompt saw the observation.
    assert len(runner.prompts) == 2
    assert "obs-7" in runner.prompts[1]
    # The worker attached the trusted observation identity to the evidence.
    assert any(item.get("observation_id") == "obs-7" for item in result.evidence)
    assert result.usage["tokens"] == 20 and result.usage_complete is True


def test_loop_stops_at_the_step_limit_and_reports_why() -> None:
    tool_turn = json.dumps({"action": "tool", "tool": "investigation_query",
                            "arguments": {"kind": "policy", "arguments": {}}})
    runner = ScriptedRunner([tool_turn] * 3)
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box(), max_steps=3)
    result = worker.run(_request())
    assert result.status == "needs_input"
    assert result.summary == "investigation_step_limit_reached"
    assert len(runner.prompts) == 3


def test_loop_enforces_the_token_budget_from_the_provider_receipt() -> None:
    runner = ScriptedRunner(
        [json.dumps({"action": "tool", "tool": "investigation_query",
                     "arguments": {"kind": "policy", "arguments": {}}})],
        tokens=900)
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box())
    with pytest.raises(WorkerError, match="worker_token_budget_exceeded"):
        worker.run(_request(budget_tokens=500))


def test_loop_treats_a_denied_tool_as_terminal() -> None:
    runner = ScriptedRunner([
        json.dumps({"action": "tool", "tool": "investigation_query",
                    "arguments": {"kind": "policy", "arguments": {}}}),
        json.dumps({"action": "final", "report": {"status": "completed",
                                                   "summary": "should not run"}}),
    ])
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box())
    # Naming a tool the worker does not hold narrows its tool set to nothing.
    result = worker.run(_request(tools=("web_search",)))
    assert result.status == "failed"
    assert result.summary.startswith("tool_denied:")
    assert len(runner.prompts) == 1


def test_cancellation_is_checked_before_every_tool_call() -> None:
    runner = ScriptedRunner([
        json.dumps({"action": "tool", "tool": "investigation_query",
                    "arguments": {"kind": "policy", "arguments": {}}})])
    calls = {"n": 0}

    def should_stop():
        calls["n"] += 1
        return calls["n"] > 1  # allow the first check, stop before the tool call

    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box(),
                            should_stop=should_stop)
    with pytest.raises(WorkerCancelled):
        worker.run(_request())


def test_invalid_model_output_fails_closed() -> None:
    runner = ScriptedRunner(["not json at all"])
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box())
    result = worker.run(_request())
    assert result.status == "failed" and result.summary == "worker_output_invalid"


def test_unknown_tool_is_a_protocol_error_not_a_provider_call() -> None:
    runner = ScriptedRunner([
        json.dumps({"action": "tool", "tool": "drop_database", "arguments": {}})])
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box())
    with pytest.raises(WorkerError, match="tool_unavailable"):
        worker.run(_request())


def test_usage_is_marked_unknown_when_the_provider_does_not_report_it() -> None:
    runner = ScriptedRunner([
        json.dumps({"action": "final", "report": {"status": "completed",
                                                   "summary": "ok"}})], reports_usage=False)
    worker = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box())
    result = worker.run(_request())
    assert result.usage_complete is False


def test_scope_is_derived_from_the_request_object() -> None:
    scope = _scope_for(_request())
    assert scope.contains("purchase.order:7")
    assert not scope.contains("purchase.order:8")
    assert _scope_for(_request(object_ref=None)).scope_mode == ObjectScope.all_objects().scope_mode


def test_dsh_worker_is_explicitly_a_summariser_not_an_investigator() -> None:
    from forgeflow.superagent import DshWorker

    worker = DshWorker("dsh", {"assess"}, lambda prompt: json.dumps(
        {"status": "completed", "summary": "summary only"}))
    assert worker.summary_only is True
    assert ToolLoopWorker("pi", {"assess"}, runner=ScriptedRunner([]),
                          tools=_box()).summary_only is False


class _Quotas:
    def __init__(self, allowed: bool):
        self.allowed = allowed
        self.calls = []

    def consume(self, tenant, kind, amount):
        self.calls.append((tenant, kind, amount))
        return {"allowed": self.allowed}


def test_an_exhausted_tool_quota_stops_before_the_tool_runs() -> None:
    calls = []
    box = InvestigationToolBox(lambda action, payload: calls.append(action) or {})
    runner = ScriptedRunner([
        json.dumps({"action": "tool", "tool": "investigation_query",
                    "arguments": {"kind": "record",
                                  "arguments": {"entity": "purchase_order", "id": 7}},
                    "why": "look"})])
    quotas = _Quotas(allowed=False)
    result = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=box,
                            quotas=quotas).run(_request())
    assert result.status == "needs_input"
    assert result.summary == "tool_calls_quota_exhausted"
    assert result.attention and result.attention[0]["need"] == "budget"
    assert calls == [] and quotas.calls == [("tenant-a", "tool_calls", 1)]


def test_an_allowed_tool_quota_is_consumed_once_per_tool_call() -> None:
    runner = ScriptedRunner([
        json.dumps({"action": "tool", "tool": "investigation_query",
                    "arguments": {"kind": "record",
                                  "arguments": {"entity": "purchase_order", "id": 7}},
                    "why": "look"}),
        json.dumps({"action": "final", "report": {"status": "completed",
                                                  "summary": "done",
                                                  "findings": [], "evidence": [],
                                                  "unknowns": [], "attention": []}})])
    quotas = _Quotas(allowed=True)
    result = ToolLoopWorker("pi", {"assess"}, runner=runner, tools=_box(),
                            quotas=quotas).run(_request())
    assert result.status == "completed"
    assert quotas.calls == [("tenant-a", "tool_calls", 1)]


def test_a_direct_report_object_is_accepted_as_the_final_action() -> None:
    from forgeflow.superagent.workers import _parse_action
    parsed = _parse_action(json.dumps({"status": "completed", "summary": "done",
                                      "findings": [], "evidence": [],
                                      "unknowns": [], "attention": []}))
    assert parsed == {"action": "final", "report": {
        "status": "completed", "summary": "done", "findings": [], "evidence": [],
        "unknowns": [], "attention": []}}
    assert _parse_action('{"status": "completed"}') is None


def test_tool_calls_are_limited_by_the_tenant_quota() -> None:
    import os
    import uuid

    from forgeflow.superagent import ControlStore
    from forgeflow.superagent.quotas import QuotaLimits, QuotaStore

    database_url = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
    if not database_url:
        pytest.skip("FORGEFLOW_CONTROL_DATABASE_URL not set")
    store = ControlStore(database_url)
    store.migrate()
    tenant = f"tool-quota-{uuid.uuid4().hex}"
    store.seed_tenant(tenant, "Tool quota", 1, (("planner", "Planner", "planner"),))
    quotas = QuotaStore(store, QuotaLimits(tenant={"tool_calls": 1}))
    action = json.dumps({"action": "tool", "tool": "investigation_query",
                         "arguments": {"kind": "record",
                                       "arguments": {"entity": "purchase_order", "id": 7}},
                         "why": "read the order"})
    try:
        worker = ToolLoopWorker("pi", {"assess"}, runner=ScriptedRunner([action, action]),
                                tools=_box(), quotas=quotas)
        result = worker.run(_request(tenant_id=tenant))
        assert result.status == "needs_input"
        assert result.summary == "tool_calls_quota_exhausted"
        assert result.attention[0]["need"] == "budget"
        usage = store.connection.execute(
            "SELECT used FROM control_quota_usage WHERE tenant_id=%s AND kind=%s",
            (tenant, "tool_calls")).fetchone()
        store.connection.commit()
        assert int(usage["used"]) == 1
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_quota_usage WHERE tenant_id = ANY(%s)",
                ([tenant, "*"],))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()

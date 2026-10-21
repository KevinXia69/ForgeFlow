"""Failure facts remain bounded and never authorize a second write."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from forgeflow.executor.protocol import ProtocolError, error_payload
from forgeflow.product.state import failed_execution
from forgeflow.recovery import client
from forgeflow.recovery.failure import cause_summary, failure_projection
from forgeflow.recovery.journal import ExecutionStopped, Step, execute
from forgeflow.recovery.readback import ReadbackError

PLAN = "a" * 64
CANARY = "private-canary-key-123"


def failure(**changes):
    context = {"stage": "observe_after", "reason": "mutation_outcome_unknown",
               "step_number": 3, "step_kind": "schedule_workorder", "planned_steps": 7,
               "dispatch_count": 3, "confirmed_steps": 2, "pending_dispatch": True,
               "cause": "invalid_response", "cause_code": None, "reported_http_status": 503}
    context.update(changes)
    return {"schema_version": 1, "plan_id": PLAN, "context": context}


def test_partial_execution_failure_reports_the_confirmed_prefix_without_retry(tmp_path):
    class ERP:
        def __init__(self):
            self.records = set()
            self.writes = []

        def preflight(self, *_):
            pass

        def observe(self, step):
            if len(self.writes) == 3:
                raise ValueError("Unexpected status code 503: " + CANARY)
            return "after" if step.key in self.records else "before"

        def apply(self, step):
            self.writes.append(step.key)
            self.records.add(step.key)

    erp = ERP()
    steps = tuple(Step(str(n), "schedule_workorder", f"workorder:{n}", {}) for n in range(1, 8))
    journal = tmp_path / "journal.jsonl"
    with pytest.raises(ExecutionStopped, match="^mutation_outcome_unknown$") as caught:
        execute(steps, erp, journal)
    assert caught.value.context == failure()["context"]
    assert erp.writes == ["1", "2", "3"]
    assert len(journal.read_text().splitlines()) == 6
    assert CANARY not in json.dumps(caught.value.context) + str(caught.value)


@pytest.mark.parametrize("change", [
    {"stage": CANARY}, {"stage": {}}, {"stage": "authorize"}, {"reason": CANARY}, {"step_kind": []},
    {"extra": CANARY}, {"step_number": True}, {"step_number": 8}, {"step_number": None},
    {"confirmed_steps": 8}, {"dispatch_count": -1}, {"planned_steps": 10001},
    {"pending_dispatch": 1}, {"dispatch_count": None}, {"confirmed_steps": 7},
    {"cause": CANARY}, {"cause_code": CANARY}, {"reported_http_status": 999},
    {"reported_http_status": "503"}, {"cause": "timeout"},
])
def test_unknown_extra_and_contradictory_fields_never_reach_product_or_transport(change):
    value = failure(**change)
    assert failure_projection(value) is None
    assert "failure" not in failed_execution(failure=value, plan_id=PLAN)
    error = ProtocolError("execution_stopped", 409, failure=value)
    assert "failure" not in error_payload("apply", error)["error"]
    with pytest.raises(client.ExecutorError) as caught:
        client._raise_server_error({"error": {"code": "execution_stopped", "status": 409,
                                             "reason": "mutation_outcome_unknown", "retryable": False,
                                             "failure": value}})
    assert caught.value.code == "bad_response" and CANARY not in str(caught.value)


def test_failure_binding_and_closed_envelope():
    value = failure()
    assert failure_projection(value, plan_id=PLAN) == value
    assert failure_projection(value, plan_id="b" * 64) is None
    assert "failure" not in failed_execution(failure=value, plan_id="b" * 64)
    assert failure_projection({**value, "private": CANARY}) is None
    assert failure_projection({**value, "schema_version": True}) is None
    assert failure_projection({**value, "plan_id": CANARY}) is None
    assert failure_projection({**value, "context": []}) is None
    projected = failure_projection(value)
    projected["context"]["dispatch_count"] = 0
    assert value["context"]["dispatch_count"] == 3


@pytest.mark.parametrize("error,expected", [
    (TimeoutError(CANARY), {"cause": "timeout", "cause_code": None, "reported_http_status": None}),
    (ConnectionError(CANARY), {"cause": "connection", "cause_code": None, "reported_http_status": None}),
    (ValueError(CANARY), {"cause": "invalid_response", "cause_code": None, "reported_http_status": None}),
    (ValueError("Unexpected status code 503: " + CANARY),
     {"cause": "invalid_response", "cause_code": None, "reported_http_status": 503}),
    (ReadbackError("incomplete_read"),
     {"cause": "readback_refusal", "cause_code": "incomplete_read", "reported_http_status": None}),
    (ReadbackError(CANARY), {"cause": "readback_refusal", "cause_code": None, "reported_http_status": None}),
])
def test_exception_projection_never_serializes_message(error, expected):
    assert cause_summary(error) == expected
    assert CANARY not in json.dumps(cause_summary(error))


def test_corrupt_journal_does_not_invent_zero_dispatches(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('{"corrupt": true}\n')
    step = Step("one", "schedule_workorder", "workorder:1", {})
    with pytest.raises(ExecutionStopped) as caught:
        execute((step,), object(), path)
    context = caught.value.context
    assert context["stage"] == "journal_open"
    assert context["dispatch_count"] is context["confirmed_steps"] is context["pending_dispatch"] is None


def test_wire_failure_survives_only_as_exact_closed_data():
    payload = error_payload("apply", ProtocolError("execution_stopped", 409,
                            reason="mutation_outcome_unknown", failure=failure()))
    with pytest.raises(client.ExecutorError) as caught:
        client._raise_server_error(payload)
    assert caught.value.failure == failure()
    assert "failure" not in error_payload("prepare", ProtocolError("plan_rejected", 422, failure=failure()))["error"]


@pytest.mark.parametrize("changed", [
    {"code": "approval_rejected", "reason": "approval_expired", "status": 403},
    {"retryable": True}, {"status": 200}, {"reason": "readback_failed"},
])
def test_contradictory_wire_failure_cannot_claim_a_write_was_refused(changed):
    error = {"code": "execution_stopped", "status": 409, "reason": "mutation_outcome_unknown",
             "retryable": False, "failure": failure(), **changed}
    with pytest.raises(client.ExecutorError) as caught:
        client._raise_server_error({"error": error})
    assert caught.value.code == "bad_response" and caught.value.failure is None
    projected = error_payload("apply", ProtocolError(**error))
    assert "failure" not in projected["error"]


def test_all_failure_classifications_match_the_root_protocol():
    from forgeflow.executor.protocol import classify
    from forgeflow.recovery.failure import REASONS, failure_matches_error

    for reason in REASONS:
        error = classify("apply", reason)
        # Counters are unknown for this mapping-only check.
        stages = {"readback_failed": "observe_before", "completed_step_drift": "observe_before",
                  "unresolved_previous_dispatch": "observe_before", "precondition_conflict": "observe_before",
                  "preflight_failed": "preflight", "postflight_failed": "postflight"}
        value = failure(reason=reason, stage=stages.get(reason, "observe_after"))
        assert failure_matches_error(value, code=error.code, status=error.status,
                                     reason=error.reason, retryable=error.retryable), reason


def _diagnostic_script():
    spec = importlib.util.spec_from_file_location("preflight_diagnostic_test",
            Path(__file__).resolve().parents[1] / "scripts/diagnose_product_preflight.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_legacy_log_projection_never_exposes_prose_or_claims_plan_binding(tmp_path):
    path = tmp_path / "errors.jsonl"
    rows = [{"op": "prepare", "reason": CANARY},
            {"op": "apply", "reason": "mutation_outcome_unknown", "traceback": CANARY},
            {"op": "apply", "reason": CANARY}]
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))
    result = _diagnostic_script().legacy_apply_reasons(path)
    assert result == {"scope": "sandbox_log_without_run_or_plan_binding",
                      "reason_counts": {"mutation_outcome_unknown": 1, "unclassified": 1}}
    assert CANARY not in json.dumps(result)


@pytest.mark.parametrize("kind", ["symlink", "fifo", "incomplete"])
def test_legacy_log_projection_refuses_unsafe_files(tmp_path, kind):
    import os

    path = tmp_path / "errors.jsonl"
    if kind == "symlink":
        target = tmp_path / "elsewhere"
        target.write_text(CANARY)
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path)
    else:
        path.write_text('{"op":"apply"}')
    with pytest.raises((OSError, RuntimeError)):
        _diagnostic_script().legacy_apply_reasons(path)

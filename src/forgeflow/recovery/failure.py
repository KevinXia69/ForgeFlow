"""Closed execution-failure facts; no exception prose or record contents."""
from __future__ import annotations

import re

STEP_KINDS = frozenset({
    "create_purchase", "update_purchase", "confirm_purchase", "cancel_purchase",
    "create_manufacturing", "update_manufacturing", "confirm_manufacturing",
    "cancel_manufacturing", "schedule_workorder", "bind_workorder",
})
STAGES = frozenset({
    "validate_plan", "journal_open", "journal_history", "binding_readback", "preflight",
    "observe_before", "authorize", "dispatch_record", "apply", "observe_after",
    "completion_record", "postflight",
})
REASONS = frozenset({
    "adapter_mutated_plan", "binding_receipt_changed", "binding_receipt_unavailable",
    "completed_step_drift", "execution_already_running", "incomplete_journal_record",
    "invalid_binding_receipt", "invalid_dependency_order", "invalid_journal_event",
    "invalid_journal_file", "invalid_journal_order", "invalid_journal_record",
    "invalid_journal_transition", "invalid_operation_identity", "invalid_operation_payload",
    "invalid_step_count", "journal_already_open", "journal_integrity_failure",
    "journal_not_open", "journal_plan_mismatch", "journal_size_limit", "journal_step_mismatch",
    "journal_write_failed", "mutation_not_verified", "mutation_outcome_unknown",
    "non_json_payload", "plan_too_large", "postflight_failed", "precondition_conflict",
    "preflight_failed", "readback_failed", "unresolved_previous_dispatch", "unsupported_operation",
    "details_unavailable",
})
CAUSES = frozenset({"timeout", "connection", "os_error", "invalid_response", "readback_refusal",
                   "scope_refusal", "authentication", "other"})
READBACK_REASONS = frozenset({
    "whole_scope_guard_required", "invalid_binding_directory", "invalid_execution_plan_digest",
    "invalid_binding_artifact", "binding_identity_mismatch", "incomplete_bound_workorder",
    "workorder_binding_precondition", "binding_write_incomplete", "missing_durable_binding",
    "binding_step_changed", "unsupported_read_model", "incomplete_read", "unsupported_target_model",
    "expected_one_unexecuted_workorder", "workorder_execution_or_parent_conflict",
    "workorder_identity_changed", "document_identity_not_unique", "document_name_changed",
    "expected_one_purchase_line", "execution_plan_digest_changed", "missing_dispatch_target",
    "unsupported_manufacturing_update", "purchase_line_identity_changed",
    "unsupported_purchase_update", "unsupported_dispatch_operation",
})
_FIELDS = frozenset({"stage", "reason", "step_number", "step_kind", "planned_steps", "dispatch_count",
                     "confirmed_steps", "pending_dispatch", "cause", "cause_code", "reported_http_status"})
_REASON_STAGES = {
    "mutation_outcome_unknown": "observe_after", "mutation_not_verified": "observe_after",
    "readback_failed": "observe_before", "completed_step_drift": "observe_before",
    "unresolved_previous_dispatch": "observe_before", "precondition_conflict": "observe_before",
    "preflight_failed": "preflight", "postflight_failed": "postflight",
}


def cause_summary(error):
    """A reported HTTP status is a bounded hint, never proof of write outcome."""
    result = {"cause": None, "cause_code": None, "reported_http_status": None}
    if error is None:
        return result
    kind = type(error)
    module, name = kind.__module__, kind.__name__
    if isinstance(error, TimeoutError) or (module.startswith("httpx") and name in {
        "TimeoutException", "ConnectTimeout", "ReadTimeout", "WriteTimeout", "PoolTimeout",
    }):
        result["cause"] = "timeout"
    elif isinstance(error, ConnectionError) or (module.startswith("httpx") and name in {
        "ConnectError", "ReadError", "WriteError", "NetworkError", "RemoteProtocolError",
    }):
        result["cause"] = "connection"
    elif isinstance(error, OSError):
        result["cause"] = "os_error"
    elif module == "forgeflow.recovery.readback" and name == "ReadbackError":
        result["cause"] = "readback_refusal"
        if str(error) in READBACK_REASONS:
            result["cause_code"] = str(error)
    elif module == "forgeflow.recovery.scope_guard" and name == "ScopeError":
        result["cause"] = "scope_refusal"
    elif module == "odoolib.tools" and name == "AuthenticationError":
        result["cause"] = "authentication"
    elif kind in {ValueError, TypeError, KeyError, IndexError} or (module == "json.decoder" and name == "JSONDecodeError"):
        result["cause"] = "invalid_response"
        # odoolib JSON-2 raises ValueError with this fixed prefix. Discard all
        # following text, which may contain ERP data, credentials or a traceback.
        if kind is ValueError:
            match = re.match(r"Unexpected status code ([45][0-9]{2}):", str(error))
            if match:
                result["reported_http_status"] = int(match[1])
    else:
        result["cause"] = "other"
    return result


def context_projection(value):
    """Reject extra fields and contradictory/overlarge counters as a unit."""
    if not isinstance(value, dict) or set(value) != _FIELDS:
        return None
    if any(value[key] is not None and not isinstance(value[key], str)
           for key in ("stage", "reason", "step_kind", "cause", "cause_code")):
        return None
    if value["stage"] not in STAGES or value["reason"] not in REASONS:
        return None
    expected_stage = _REASON_STAGES.get(value["reason"])
    if expected_stage is not None and value["stage"] != expected_stage:
        return None
    for key in ("step_number", "planned_steps", "dispatch_count", "confirmed_steps"):
        item = value[key]
        if item is not None and (type(item) is not int or not 0 <= item <= 10000):
            return None
    total, step = value["planned_steps"], value["step_number"]
    if (step is None) != (value["step_kind"] is None):
        return None
    if step is not None and (total is None or not 1 <= step <= total or value["step_kind"] not in STEP_KINDS):
        return None
    for key in ("dispatch_count", "confirmed_steps"):
        if value[key] is not None and (total is None or value[key] > total):
            return None
    if value["pending_dispatch"] is not None and type(value["pending_dispatch"]) is not bool:
        return None
    known = [value[key] is not None for key in ("dispatch_count", "confirmed_steps", "pending_dispatch")]
    if any(known) and not all(known):
        return None
    if value["pending_dispatch"] and (not value["dispatch_count"] or value["confirmed_steps"] == total):
        return None
    if value["cause"] is not None and value["cause"] not in CAUSES:
        return None
    if value["cause_code"] is not None and (value["cause"] != "readback_refusal"
                                           or value["cause_code"] not in READBACK_REASONS):
        return None
    status = value["reported_http_status"]
    if status is not None and (type(status) is not int or not 400 <= status <= 599
                               or value["cause"] != "invalid_response"):
        return None
    return dict(value)


def failure_projection(value, *, plan_id=None):
    if not isinstance(value, dict) or set(value) != {"schema_version", "plan_id", "context"}:
        return None
    if (type(value["schema_version"]) is not int or value["schema_version"] != 1
            or not isinstance(value["plan_id"], str) or not re.fullmatch(r"[a-f0-9]{64}", value["plan_id"])
            or (plan_id is not None and value["plan_id"] != plan_id)):
        return None
    context = context_projection(value["context"])
    return {"schema_version": 1, "plan_id": value["plan_id"], "context": context} if context else None


def failure_matches_error(value, *, code, status, reason, retryable):
    """Do not attach a pending-write diagnostic to an approval rejection."""
    projected = failure_projection(value)
    if projected is None:
        return False
    stopped = {"mutation_outcome_unknown", "unresolved_previous_dispatch", "readback_failed",
               "preflight_failed", "postflight_failed"}
    conflicts = {"precondition_conflict", "completed_step_drift", "mutation_not_verified"}
    detail = projected["context"]["reason"]
    if detail in stopped:
        expected = ("execution_stopped", 409, detail, False)
    elif detail in conflicts:
        expected = ("execution_conflict", 409, detail, False)
    elif detail == "journal_integrity_failure":
        expected = ("store_corrupt", 500, detail, False)
    elif detail == "execution_already_running":
        expected = ("executor_busy", 503, detail, True)
    else:
        expected = ("execution_conflict", 409, None, False)
    return (code, status, reason, retryable) == expected

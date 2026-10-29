"""Newline-delimited JSON transport and envelope validation for the executor.

These helpers never open a socket; callers pass a binary file-like object.  A
frame is one UTF-8 JSON object followed by ``\\n`` and must fit in
:data:`MAX_FRAME_BYTES`, including the newline.
"""

from __future__ import annotations

import json
import re
from typing import IO, Any

from forgeflow.recovery.failure import failure_matches_error, failure_projection

SCHEMA_VERSION = 1
MAX_FRAME_BYTES = 65536
OPS = ("prepare", "apply", "status", "health", "reconcile", "product")
PLAN_ID = re.compile(r"[a-f0-9]{64}")
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")

_REASON = re.compile(r"[a-z0-9_:.;|-]{1,160}")
_ALLOWED_KEYS = {
    "prepare": frozenset({"schema_version", "op", "run_id", "request"}),
    "apply": frozenset({"schema_version", "op", "run_id", "plan_id", "approval"}),
    "status": frozenset({"schema_version", "op", "run_id", "plan_id"}),
    "reconcile": frozenset({"schema_version", "op", "run_id", "plan_id"}),
    "health": frozenset({"schema_version", "op"}),
    "product": frozenset({"schema_version", "op", "run_id", "action", "payload"}),
}

# reason -> (wire code, HTTP-style status, retryable).  The reason itself is
# useful for recovery callers; the wire code is the closed set they may branch
# on.  Unknown reasons are deliberately collapsed by ``classify``.
ERROR_CODES: dict[str, tuple[str, int, bool]] = {
    "invalid_plan_id": ("invalid_field", 400, False),
    "missing_original_plan": ("plan_not_found", 404, False),
    "original_plan_artifact_changed": ("plan_binding_changed", 409, False),
    "approval_expired": ("approval_rejected", 403, False),
    "approval_already_consumed": ("approval_rejected", 403, False),
    "unfinished_execution_requires_original_plan": ("scope_held", 409, False),
    "another_recovery_operation_running": ("executor_busy", 503, True),
    "mutation_outcome_unknown": ("execution_stopped", 409, False),
    "precondition_conflict": ("execution_conflict", 409, False),
    "unresolved_previous_dispatch": ("execution_stopped", 409, False),
    "journal_integrity_failure": ("store_corrupt", 500, False),
    "recovery_bundle_required": ("internal_error", 500, False),
    "execution_already_running": ("executor_busy", 503, True),
    "plan_preparation_incomplete": ("plan_not_ready", 409, False),
    "original_source_binding_changed": ("plan_binding_changed", 409, False),
    "margin_basis_requires_reprepare": ("plan_binding_changed", 409, False),
    "legacy_recovery_authorization_required": ("plan_binding_changed", 409, False),
    "original_execution_binding_changed": ("plan_binding_changed", 409, False),
    "source_bytes_changed": ("plan_binding_changed", 409, False),
    "source_file_missing": ("plan_binding_changed", 409, False),
    "invalid_source_manifest": ("plan_binding_changed", 409, False),
    "prior_execution_summary_changed": ("plan_binding_changed", 409, False),
    "approval_required": ("approval_rejected", 403, False),
    "invalid_execution_grant": ("approval_rejected", 403, False),
    "approval_run_mismatch": ("approval_rejected", 403, False),
    "approval_policy_mismatch": ("approval_rejected", 403, False),
    "approval_snapshot_mismatch": ("approval_rejected", 403, False),
    "approval_approver_mismatch": ("approval_rejected", 403, False),
    "approval_token_tampered": ("approval_rejected", 403, False),
    "approval_not_found": ("approval_rejected", 403, False),
    "approval_marker_write_failed": ("internal_error", 500, False),
    "task_contract_mismatch": ("plan_rejected", 422, False),
    "plan_contract_violation": ("plan_rejected", 422, False),
    "policy_file_untrusted": ("store_corrupt", 500, False),
    "executor_shutting_down": ("execution_stopped", 409, False),
    "readback_failed": ("execution_stopped", 409, False),
    "preflight_failed": ("execution_stopped", 409, False),
    "postflight_failed": ("execution_stopped", 409, False),
    "final_document_readback_failed": ("execution_conflict", 409, False),
    "completed_step_drift": ("execution_conflict", 409, False),
    "mutation_not_verified": ("execution_conflict", 409, False),
}
for _reason in (
    "product_version_required",
    "product_dispatch_present", "product_cancellation_unconfirmed",
    "product_revision_conflict", "product_operation_conflict", "product_already_authorized",
    "product_preparation_interrupted", "product_candidate_invalid", "product_binding_changed",
    "product_comparison_facts_mismatch", "product_plan_retired", "product_selection_changed",
    "product_plan_not_selected", "product_approval_required", "product_approval_mismatch",
    "product_history_limit",
    "product_constraints_relaxed", "product_constraints_binding_changed",
    "product_objective_binding_changed",
):
    ERROR_CODES[_reason] = ("product_conflict", 409, False)
ERROR_CODES.update({
    "product_owner_mismatch": ("product_forbidden", 403, False),
    "product_invalid_request": ("invalid_field", 400, False),
    "product_decision_corrupt": ("store_corrupt", 500, False),
    "product_decision_missing": ("plan_not_found", 404, False),
    "product_constraints_unsupported": ("plan_rejected", 422, False),
    "product_constraints_invalid": ("invalid_field", 400, False),
    "product_objectives_invalid": ("invalid_field", 400, False),
    "product_candidates_unavailable": ("plan_rejected", 422, False),
    "product_constraints_must_tighten": ("plan_rejected", 422, False),
    "product_constraint_not_satisfied": ("plan_rejected", 422, False),
    "planning_model_infeasible": ("plan_rejected", 422, False),
})


class ProtocolError(Exception):
    """A fixed-code protocol failure with its safe-to-send details."""

    code: str
    status: int
    reason: str | None
    retryable: bool

    def __init__(
        self,
        code: str,
        status: int,
        reason: str | None = None,
        retryable: bool = False,
        failure: dict | None = None,
    ) -> None:
        super().__init__(code)
        self.code = code
        self.status = status
        self.reason = reason
        self.retryable = retryable
        self.failure = failure_projection(failure)

    def to_dict(self, op: str) -> dict[str, Any]:
        """Render the stable error envelope for ``op``."""
        return error_payload(op, self)


def _safe_reason(reason: str | None) -> str | None:
    if isinstance(reason, str) and _REASON.fullmatch(reason):
        return reason
    return None


def error_payload(op: str, error: ProtocolError) -> dict[str, Any]:
    """Build the stable wire response for a protocol error."""
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "op": op,
        "error": {
            "code": error.code,
            "status": error.status,
            "retryable": error.retryable,
            "reason": _safe_reason(error.reason),
        },
    }
    failure = failure_projection(error.failure)
    if op == "apply" and failure_matches_error(failure, code=error.code, status=error.status,
                                               reason=error.reason, retryable=error.retryable):
        payload["error"]["failure"] = failure
    return payload


def _reject_constant(value: str) -> None:
    # json.loads accepts NaN and Infinity by default; neither is valid JSON.
    raise ValueError(f"non-finite number: {value}")


def read_frame(stream: IO[bytes]) -> dict[str, Any]:
    """Read exactly one bounded JSON-object frame from ``stream``.

    Bytes after the first newline are left unread.  Rejections are ordered:
    oversized frame, missing newline, invalid UTF-8, invalid JSON, non-object.
    """
    raw = stream.readline(MAX_FRAME_BYTES + 1)
    if not isinstance(raw, bytes):
        raise ProtocolError("bad_request", 400)
    if len(raw) > MAX_FRAME_BYTES:
        raise ProtocolError("body_too_large", 413)
    if not raw.endswith(b"\n"):
        raise ProtocolError("bad_request", 400)
    try:
        text = raw[:-1].decode("utf-8")
    except UnicodeDecodeError:
        raise ProtocolError("bad_request", 400) from None
    try:
        body = json.loads(text, parse_constant=_reject_constant)
    except (TypeError, ValueError):
        raise ProtocolError("bad_request", 400) from None
    if not isinstance(body, dict):
        raise ProtocolError("bad_request", 400)
    return body


def write_frame(stream: IO[bytes], payload: dict[str, Any]) -> None:
    """Write one canonical, compact JSON frame, enforcing the byte limit."""
    try:
        encoded = json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        raise ProtocolError("internal_error", 500) from None
    frame = encoded + b"\n"
    if len(frame) > MAX_FRAME_BYTES:
        raise ProtocolError("response_too_large", 500)
    stream.write(frame)


def validate_envelope(body: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Validate and strip the common envelope from a request object.

    Returns the operation and the remaining operation-specific body.  Only the
    operation's declared keys are permitted; ``request`` internals are checked
    by the request validator, not here.
    """
    version = body.get("schema_version")
    if type(version) is not int or version != SCHEMA_VERSION:
        raise ProtocolError("unsupported_schema_version", 400)
    op = body.get("op")
    if not isinstance(op, str) or op not in OPS:
        raise ProtocolError("unknown_operation", 404)
    if set(body) - _ALLOWED_KEYS[op]:
        raise ProtocolError("unknown_key", 400)
    if "run_id" in body:
        run_id = body["run_id"]
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise ProtocolError("invalid_field", 400)
    if "plan_id" in body:
        plan_id = body["plan_id"]
        if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
            raise ProtocolError("invalid_field", 400)
    if "approval" in body and body["approval"] is not None:
        if not isinstance(body["approval"], dict):
            raise ProtocolError("invalid_field", 400)
    if op == "product" and (not isinstance(body.get("action"), str)
                            or body.get("action") not in {"prepare", "decide", "authorize", "status", "cancel_unstarted", "artifact"}
                            or not isinstance(body.get("payload"), dict)):
        raise ProtocolError("invalid_field", 400)
    return op, {
        key: value for key, value in body.items() if key not in {"schema_version", "op"}
    }


def classify(op: str, reason: str) -> ProtocolError:
    """Map a recovery failure reason onto the closed executor error set."""
    spec = ERROR_CODES.get(reason)
    if spec is not None:
        code, status, retryable = spec
        return ProtocolError(code, status, reason=reason, retryable=retryable)
    if op == "prepare":
        return ProtocolError("plan_rejected", 422)
    return ProtocolError("execution_conflict", 409)

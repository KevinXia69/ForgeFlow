"""Agent-side client for the root executor's one-line JSON Unix-socket protocol.

The agent sends only business intent; the executor owns credentials, plans and
writes. Transport and response details never appear in error messages, because
they may contain paths or data the agent should not see.
"""

from __future__ import annotations

import json
import os
import re
import socket
import time

from forgeflow.recovery.failure import failure_matches_error, failure_projection

DEFAULT_SOCKET = "/run/forgeflow/executor.sock"
MAX_RESPONSE_BYTES = 65536
SCHEMA_VERSION = 1

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_PLAN_ID = re.compile(r"[a-f0-9]{64}")


class ExecutorError(RuntimeError):
    """A fixed-code executor failure; ``unavailable`` means transport failure."""

    def __init__(
        self, code: str, *, status: int = 0, retryable: bool = False,
        reason: str | None = None,
        failure: dict | None = None,
    ) -> None:
        self.code = code
        self.status = status
        self.retryable = retryable
        self.reason = reason
        self.failure = failure_projection(failure)
        super().__init__(f"{code}: {reason}" if reason else code)



# This is a model-facing projection, stricter than the transport envelope.
# Never echo arbitrary server reason strings, even if they are valid wire text.
PREPARE_ERROR_CODES = frozenset({
    "plan_rejected", "invalid_field", "scope_held", "executor_busy", "store_corrupt",
    "internal_error", "unavailable", "bad_response", "plan_binding_changed",
    "prepare_rejected",
})
CHANGE_SET_REASONS = frozenset({
    "change_set_binding_mismatch", "change_set_supply_quantity_mismatch",
    "change_set_existing_offer_ambiguous", "change_set_mapping_unproven",
    "change_set_origin_capacity_mismatch",
})
PREPARE_REASONS = frozenset({
    "task_contract_mismatch", "plan_contract_violation", "change_set_rejected",
    "live_input_rejected", "unselected_active_demand", "broken_offer_ambiguous",
    "broken_offer_not_found", "unfinished_execution_requires_original_plan",
    "another_recovery_operation_running", "recovery_bundle_required",
    "policy_file_untrusted", "source_bytes_changed", "source_file_missing",
    "invalid_source_manifest", "details_unavailable",
}) | CHANGE_SET_REASONS


def prepare_feedback(error: ExecutorError) -> dict:
    """Return fixed codes and bounded positive IDs, never exception prose."""
    code = error.code if error.code in PREPARE_ERROR_CODES else "prepare_rejected"
    reason = error.reason if isinstance(error.reason, str) else ""
    # The live planner's fixed internal name predates the public feedback name.
    # Normalize only that exact rejection; never forward an appended offer key.
    if code == "plan_rejected" and reason == "broken_offer_missing":
        reason = "broken_offer_not_found"
    if reason in CHANGE_SET_REASONS and code != "plan_rejected":
        reason = "details_unavailable"
    ids: list[int] = []
    prefix = "unselected_active_demand:"
    if reason.startswith(prefix) and len(reason) <= 160:
        raw = reason[len(prefix):].split("-")
        if (1 <= len(raw) <= 20
                and all(re.fullmatch(r"[1-9][0-9]{0,15}", item) for item in raw)):
            parsed = [int(item) for item in raw]
            if all(value <= 9007199254740991 for value in parsed):
                ids = list(dict.fromkeys(parsed))
                reason = "unselected_active_demand"
    if reason not in PREPARE_REASONS:
        reason = "details_unavailable"
    if code != "plan_rejected":
        ids = []
    return {"code": code, "reason": reason, "missing_sales_order_ids": ids}


def _unavailable(reason: str) -> ExecutorError:
    return ExecutorError("unavailable", retryable=True, reason=reason)


def _bad_response(reason: str) -> ExecutorError:
    return ExecutorError("bad_response", reason=reason)


def _canonical_line(payload: dict) -> bytes:
    try:
        text = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError):
        raise ExecutorError("invalid_field", reason="payload is not JSON") from None
    return text.encode("utf-8") + b"\n"


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise _unavailable("timeout")
    return left


def _read_line(sock: socket.socket, deadline: float) -> bytes:
    data = bytearray()
    while True:
        try:
            sock.settimeout(_remaining(deadline))
            chunk = sock.recv(min(4096, MAX_RESPONSE_BYTES + 1 - len(data)))
        except ExecutorError:
            raise
        except (OSError, ValueError):
            raise _unavailable("transport failure") from None
        if not chunk:
            raise _unavailable("connection closed")
        newline = chunk.find(b"\n")
        if newline >= 0:
            if len(data) + newline + 1 > MAX_RESPONSE_BYTES:
                raise _bad_response("response too large")
            data.extend(chunk[: newline + 1])
            return bytes(data)
        data.extend(chunk)
        if len(data) >= MAX_RESPONSE_BYTES:
            raise _bad_response("response too large")


def _raise_server_error(response: dict) -> None:
    error = response.get("error")
    if not isinstance(error, dict):
        raise _bad_response("malformed response")
    code = error.get("code")
    status = error.get("status")
    retryable = error.get("retryable")
    reason = error.get("reason")
    if (
        not isinstance(code, str) or not code
        or isinstance(status, bool) or not isinstance(status, int)
        or not isinstance(retryable, bool)
        or (reason is not None and not isinstance(reason, str))
    ):
        raise _bad_response("malformed response")
    failure = failure_projection(error.get("failure"))
    if "failure" in error and not failure_matches_error(failure, code=code, status=status,
                                                        reason=reason, retryable=retryable):
        raise _bad_response("invalid failure details")
    raise ExecutorError(code, status=status, retryable=retryable, reason=reason, failure=failure)


def request(socket_path: str, payload: dict, *, timeout: float) -> dict:
    """Send one canonical JSON request line and decode one JSON response line."""
    line = _canonical_line(payload)
    deadline = time.monotonic() + timeout
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(_remaining(deadline))
            sock.connect(os.fspath(socket_path))
            sock.settimeout(_remaining(deadline))
            sock.sendall(line)
            raw = _read_line(sock, deadline)
    except ExecutorError:
        raise
    except (OSError, ValueError):
        raise _unavailable("transport failure") from None
    try:
        response = json.loads(raw)
    except (UnicodeDecodeError, ValueError):
        raise _bad_response("malformed response") from None
    if not isinstance(response, dict):
        raise _bad_response("malformed response")
    if response.get("ok") is True:
        return response
    if response.get("ok") is False:
        _raise_server_error(response)
    raise _bad_response("malformed response")


def _check_run_id(run_id: object) -> None:
    if not isinstance(run_id, str) or _RUN_ID.fullmatch(run_id) is None:
        raise ExecutorError("invalid_field", reason="invalid run_id")


def _check_plan_id(plan_id: object) -> None:
    if not isinstance(plan_id, str) or _PLAN_ID.fullmatch(plan_id) is None:
        raise ExecutorError("invalid_field", reason="invalid plan_id")


def _envelope(op: str, **fields: object) -> dict:
    return {"schema_version": SCHEMA_VERSION, "op": op, **fields}


def prepare(
    socket_path: str, run_id: str, request_body: dict, *, timeout: float = 180.0
) -> dict:
    _check_run_id(run_id)
    if not isinstance(request_body, dict):
        raise ExecutorError("invalid_field", reason="invalid request")
    return request(
        socket_path, _envelope("prepare", run_id=run_id, request=request_body), timeout=timeout
    )


def product(socket_path: str, run_id: str, action: str, payload: dict, *, timeout: float = 300) -> dict:
    """Product-only root decisions; autonomous executors reject this operation."""
    _check_run_id(run_id)
    if (not isinstance(action, str) or action not in {"prepare", "decide", "authorize", "status", "cancel_unstarted", "artifact"}
            or not isinstance(payload, dict)):
        raise ExecutorError("invalid_field", reason="invalid product request")
    return request(socket_path, _envelope("product", run_id=run_id, action=action,
                                         payload=payload), timeout=timeout)


def apply(
    socket_path: str, run_id: str, plan_id: str, approval: dict | None = None,
    *, timeout: float = 900.0,
) -> dict:
    _check_run_id(run_id)
    _check_plan_id(plan_id)
    if approval is not None and not isinstance(approval, dict):
        raise ExecutorError("invalid_field", reason="invalid approval")
    payload = _envelope(
        "apply", run_id=run_id, plan_id=plan_id, approval=approval
    )
    return request(socket_path, payload, timeout=timeout)


def status(socket_path: str, run_id: str, plan_id: str, *, timeout: float = 5.0) -> dict:
    _check_run_id(run_id)
    _check_plan_id(plan_id)
    return request(
        socket_path, _envelope("status", run_id=run_id, plan_id=plan_id), timeout=timeout
    )


def health(socket_path: str, *, timeout: float = 5.0) -> dict:
    return request(socket_path, _envelope("health"), timeout=timeout)


def reconcile(socket_path: str, run_id: str, plan_id: str, *, timeout: float = 900.0) -> dict:
    """Product-only fresh readback/verifier recovery; never sends an approval."""
    _check_run_id(run_id)
    _check_plan_id(plan_id)
    return request(socket_path, _envelope("reconcile", run_id=run_id, plan_id=plan_id), timeout=timeout)

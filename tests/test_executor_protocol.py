"""Tests for the executor's newline-delimited JSON transport and envelopes."""

from __future__ import annotations

from io import BytesIO

import pytest

from forgeflow.executor.protocol import (
    ERROR_CODES,
    MAX_FRAME_BYTES,
    OPS,
    SCHEMA_VERSION,
    ProtocolError,
    classify,
    error_payload,
    read_frame,
    validate_envelope,
    write_frame,
)

RUN_ID = "run-1"
PLAN_ID = "a" * 64
WIRE_CODES = set(
    "bad_request unknown_key unsupported_schema_version invalid_field "
    "body_too_large unknown_operation plan_not_found plan_not_ready "
    "plan_binding_changed plan_run_mismatch approval_rejected scope_held "
    "executor_busy execution_stopped execution_conflict plan_rejected "
    "store_corrupt internal_error unavailable product_conflict product_forbidden "
    "product_operations_disabled".split()
)


class SpyStream(BytesIO):
    """A stream that records how many times readline was called."""

    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.readline_calls = 0

    def readline(self, size: int = -1) -> bytes:
        self.readline_calls += 1
        return super().readline(size)


def rejected(payload: bytes, code: str, status: int) -> ProtocolError:
    with pytest.raises(ProtocolError) as caught:
        read_frame(BytesIO(payload))
    assert (caught.value.code, caught.value.status) == (code, status)
    return caught.value


def test_read_frame_accepts_valid_line() -> None:
    assert read_frame(BytesIO(b'{"a":1}\n')) == {"a": 1}


def test_read_frame_rejects_missing_newline() -> None:
    rejected(b'{"a":1}', "bad_request", 400)


def test_read_frame_rejects_oversize_including_newline() -> None:
    rejected(b"x" * MAX_FRAME_BYTES + b"\n", "body_too_large", 413)


def test_read_frame_checks_size_before_json() -> None:
    rejected(b"x" * (MAX_FRAME_BYTES - 1) + b"\n", "bad_request", 400)


def test_read_frame_rejects_invalid_utf8() -> None:
    rejected(b'{"a":"\xff"}\n', "bad_request", 400)


def test_read_frame_rejects_nan() -> None:
    rejected(b'{"a":NaN}\n', "bad_request", 400)


def test_read_frame_rejects_array_instead_of_object() -> None:
    rejected(b"[]\n", "bad_request", 400)


def test_read_frame_ignores_bytes_after_first_newline() -> None:
    stream = SpyStream(b'{"a":1}\n{"b":2}\n')
    assert read_frame(stream) == {"a": 1}
    assert stream.readline_calls == 1
    assert stream.tell() == len(b'{"a":1}\n')


def test_validate_envelope_accepts_health() -> None:
    assert validate_envelope({"schema_version": 1, "op": "health"}) == ("health", {})


def test_validate_envelope_rejects_unknown_operation() -> None:
    with pytest.raises(ProtocolError) as caught:
        validate_envelope({"schema_version": SCHEMA_VERSION, "op": "execute"})
    assert (caught.value.code, caught.value.status) == ("unknown_operation", 404)


def test_validate_envelope_rejects_schema_two() -> None:
    with pytest.raises(ProtocolError) as caught:
        validate_envelope({"schema_version": 2, "op": "health"})
    assert (caught.value.code, caught.value.status) == (
        "unsupported_schema_version",
        400,
    )


def test_validate_envelope_rejects_unknown_key() -> None:
    with pytest.raises(ProtocolError) as caught:
        validate_envelope(
            {"schema_version": SCHEMA_VERSION, "op": "health", "run_id": RUN_ID}
        )
    assert (caught.value.code, caught.value.status) == ("unknown_key", 400)


def test_validate_envelope_rejects_invalid_plan_id() -> None:
    body = {
        "schema_version": SCHEMA_VERSION,
        "op": "apply",
        "run_id": RUN_ID,
        "plan_id": "short",
        "approval": None,
    }
    with pytest.raises(ProtocolError) as caught:
        validate_envelope(body)
    assert (caught.value.code, caught.value.status) == ("invalid_field", 400)


def test_validate_envelope_accepts_apply_with_approval() -> None:
    approval = {"nonce": "abc"}
    body = {
        "schema_version": SCHEMA_VERSION,
        "op": "apply",
        "run_id": RUN_ID,
        "plan_id": PLAN_ID,
        "approval": approval,
    }
    op, payload = validate_envelope(body)
    assert op == "apply"
    assert payload == {"run_id": RUN_ID, "plan_id": PLAN_ID, "approval": approval}


def test_validate_envelope_does_not_inspect_request_keys() -> None:
    op, payload = validate_envelope(
        {
            "schema_version": SCHEMA_VERSION,
            "op": "prepare",
            "run_id": RUN_ID,
            "request": {"future_key": True},
        }
    )
    assert op == "prepare"
    assert payload["request"] == {"future_key": True}


def test_error_payload_sanitizes_unsafe_reason() -> None:
    error = ProtocolError("invalid_field", 400, "Invalid plan id: secret", True)
    payload = error_payload("apply", error)
    assert payload == {
        "schema_version": SCHEMA_VERSION,
        "ok": False,
        "op": "apply",
        "error": {
            "code": "invalid_field",
            "status": 400,
            "retryable": True,
            "reason": None,
        },
    }
    assert error.to_dict("apply") == payload


def test_error_payload_unknown_reason_is_none() -> None:
    error = classify("prepare", "a_reason_not_in_the_recovery_table")
    assert error.code == "plan_rejected"
    assert error_payload("prepare", error)["error"]["reason"] is None


def test_write_frame_size_guard() -> None:
    stream = BytesIO()
    with pytest.raises(ProtocolError) as caught:
        write_frame(stream, {"data": "x" * MAX_FRAME_BYTES})
    assert (caught.value.code, caught.value.status) == ("response_too_large", 500)
    assert stream.getvalue() == b""


def test_write_frame_round_trip_is_canonical() -> None:
    payload = {"b": 2, "a": {"z": None, "x": True}}
    stream = BytesIO()
    write_frame(stream, payload)
    assert stream.getvalue() == b'{"a":{"x":true,"z":null},"b":2}\n'
    assert read_frame(BytesIO(stream.getvalue())) == payload


def test_error_codes_map_to_known_wire_codes() -> None:
    for reason, (code, status, retryable) in ERROR_CODES.items():
        assert code in WIRE_CODES
        assert type(status) is int
        assert type(retryable) is bool
        error = classify("apply", reason)
        assert (error.code, error.status, error.retryable, error.reason) == (
            code,
            status,
            retryable,
            reason,
        )


@pytest.mark.parametrize(
    ("op", "code", "status"),
    [("prepare", "plan_rejected", 422), ("apply", "execution_conflict", 409)],
)
def test_classify_unknown_reason_defaults_by_operation(
    op: str, code: str, status: int
) -> None:
    error = classify(op, "a_reason_not_in_the_recovery_table")
    assert (error.code, error.status, error.retryable) == (code, status, False)


def test_ops_are_the_closed_endpoint_set() -> None:
    assert OPS == ("prepare", "apply", "status", "health", "reconcile", "product")

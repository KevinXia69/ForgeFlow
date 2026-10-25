"""Socket-level tests for the agent-side executor client."""

from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from forgeflow.recovery import client
from forgeflow.recovery.client import ExecutorError

RUN = "run-1"
PLAN = "a" * 64


def canonical(body: dict) -> bytes:
    return json.dumps(body, sort_keys=True, separators=(",", ":")).encode()


def read_line(conn: socket.socket) -> bytes:
    data = bytearray()
    while b"\n" not in data:
        chunk = conn.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
    return bytes(data)


def reply(conn: socket.socket, body: dict) -> None:
    conn.sendall(canonical(body) + b"\n")


class FakeServer:
    """Tiny one-connection-at-a-time server bound to ``address``."""

    def __init__(self, path, address, handler) -> None:
        self.path = path
        self.address = address
        self.handler = handler
        self.received: list[bytes] = []
        self.accepted = 0
        self.stopped = threading.Event()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(address)
        self.listener.listen()
        self.listener.settimeout(0.05)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        while not self.stopped.is_set():
            try:
                conn, _ = self.listener.accept()
            except TimeoutError:
                continue
            except OSError:
                return
            self.accepted += 1
            with conn:
                try:
                    self.handler(conn, self)
                except OSError:
                    pass

    def stop(self) -> None:
        self.stopped.set()
        self.listener.close()
        self.thread.join(timeout=2)
        self.path.unlink(missing_ok=True)


def success(conn: socket.socket, server: FakeServer) -> None:
    raw = read_line(conn)
    server.received.append(raw)
    op = json.loads(raw)["op"]
    reply(conn, {"schema_version": 1, "ok": True, "op": op, "result": op})


def failure(conn: socket.socket, server: FakeServer) -> None:
    read_line(conn)
    error = {
        "code": "approval_required", "status": 403,
        "retryable": False, "reason": "approval required",
    }
    reply(conn, {"schema_version": 1, "ok": False, "op": "apply", "error": error})


@pytest.fixture
def serve(tmp_path, monkeypatch):
    # chdir keeps the socket address short enough for AF_UNIX path limits,
    # while the socket file itself is still created inside tmp_path.
    monkeypatch.chdir(tmp_path)
    servers: list[FakeServer] = []

    def start(handler) -> FakeServer:
        server = FakeServer(tmp_path / "s.sock", "s.sock", handler)
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.stop()


def test_happy_path(serve):
    server = serve(success)
    approval = {"nonce": "n"}
    assert client.prepare(server.address, RUN, {"intent": "repair"}, timeout=2)["result"] == "prepare"
    assert client.apply(server.address, RUN, PLAN, approval, timeout=2)["result"] == "apply"
    assert client.status(server.address, RUN, PLAN, timeout=2)["result"] == "status"
    assert client.health(server.address, timeout=2)["result"] == "health"
    expected = [
        {"schema_version": 1, "op": "prepare", "run_id": RUN, "request": {"intent": "repair"}},
        {"schema_version": 1, "op": "apply", "run_id": RUN, "plan_id": PLAN, "approval": approval},
        {"schema_version": 1, "op": "status", "run_id": RUN, "plan_id": PLAN},
        {"schema_version": 1, "op": "health"},
    ]
    assert server.received == [canonical(body) + b"\n" for body in expected]


def test_failure(serve):
    server = serve(failure)
    with pytest.raises(ExecutorError) as raised:
        client.apply(server.address, RUN, PLAN, timeout=2)
    error = raised.value
    assert (error.code, error.status, error.retryable, error.reason) == (
        "approval_required", 403, False, "approval required",
    )


def test_product_artifact_action_reaches_the_executor_socket(serve):
    from forgeflow.executor.protocol import validate_envelope

    payload = {"owner_sha256": "b" * 64, "plan_id": PLAN,
               "name": "snapshot.json", "offset": 0}
    artifact = {"name": "snapshot.json", "offset": 0, "total_bytes": 2,
                "sha256": "c" * 64, "data_base64": "e30=", "eof": True}

    def handler(conn, server):
        raw = read_line(conn)
        server.received.append(raw)
        op, fields = validate_envelope(json.loads(raw))
        assert op == "product" and fields["action"] == "artifact"
        assert fields["payload"] == payload
        reply(conn, {"schema_version": 1, "ok": True, "op": op, "artifact": artifact})

    server = serve(handler)
    response = client.product(server.address, RUN, "artifact", payload, timeout=2)
    assert response["artifact"] == artifact
    assert server.accepted == 1


def test_refused(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(ExecutorError) as raised:
        client.health("missing.sock", timeout=1)
    assert raised.value.code == "unavailable"


def test_immediate_close(serve):
    server = serve(lambda conn, server: None)
    with pytest.raises(ExecutorError) as raised:
        client.health(server.address, timeout=1)
    assert raised.value.code == "unavailable"


@pytest.mark.parametrize(
    ("raw", "expected"), [(b"not-json\n", "bad_response"), (b'{"ok":true}', "unavailable")]
)
def test_bad_reply(serve, raw, expected):
    def handler(conn, server):
        read_line(conn)
        conn.sendall(raw)

    server = serve(handler)
    with pytest.raises(ExecutorError) as raised:
        client.health(server.address, timeout=1)
    assert raised.value.code == expected


def test_oversize(serve):
    def handler(conn, server):
        read_line(conn)
        conn.sendall(b"x" * (client.MAX_RESPONSE_BYTES + 1) + b"\n")

    server = serve(handler)
    with pytest.raises(ExecutorError) as raised:
        client.health(server.address, timeout=1)
    assert raised.value.code == "bad_response"


@pytest.mark.parametrize("call", [
    lambda path: client.prepare(path, "", {}, timeout=1),
    lambda path: client.prepare(path, None, {}, timeout=1),
    lambda path: client.status(path, RUN, "A" * 64, timeout=1),
    lambda path: client.apply(path, RUN, "a" * 63, timeout=1),
])
def test_invalid_fields(serve, call):
    server = serve(success)
    with pytest.raises(ExecutorError) as raised:
        call(server.address)
    assert raised.value.code == "invalid_field"
    assert server.accepted == 0


def test_timeout(serve):
    def handler(conn, server):
        read_line(conn)
        server.stopped.wait(2.0)

    server = serve(handler)
    started = time.monotonic()
    with pytest.raises(ExecutorError) as raised:
        client.health(server.address, timeout=0.2)
    elapsed = time.monotonic() - started
    assert raised.value.code == "unavailable"
    assert 0.1 <= elapsed < 1.0


@pytest.mark.parametrize("reason", [
    "unselected_active_demand:4-5", "private-fixture-canary", "task_contract_mismatch",
    "plan_contract_violation", "unselected_active_demand:4-private-fixture-canary",
    "unselected_active_demand:0", "unselected_active_demand:9007199254740992",
    "broken_offer_missing:private-fixture-canary",
    "broken_offer_missing\nprivate-fixture-canary",
    "unselected_active_demand:" + "-".join(str(i) for i in range(1, 22)),
])
def test_prepare_feedback_is_a_closed_projection(reason):
    projected = client.prepare_feedback(ExecutorError("plan_rejected", reason=reason))
    assert "private-fixture-canary" not in json.dumps(projected)
    if reason == "unselected_active_demand:4-5":
        assert projected == {"code": "plan_rejected", "reason": "unselected_active_demand",
                             "missing_sales_order_ids": [4, 5]}
    elif reason in {"task_contract_mismatch", "plan_contract_violation"}:
        assert projected["reason"] == reason
        assert projected["missing_sales_order_ids"] == []
    else:
        assert projected["reason"] == "details_unavailable"
        assert projected["missing_sales_order_ids"] == []


def test_prepare_feedback_unknown_wire_code_never_reaches_model():
    projected = client.prepare_feedback(ExecutorError("private-fixture-canary", reason="secret"))
    assert projected == {"code": "prepare_rejected", "reason": "details_unavailable",
                         "missing_sales_order_ids": []}


def test_missing_offer_alias_does_not_accept_unknown_wire_code():
    projected = client.prepare_feedback(
        ExecutorError("private-fixture-canary", reason="broken_offer_missing")
    )
    assert projected == {"code": "prepare_rejected", "reason": "details_unavailable",
                         "missing_sales_order_ids": []}


@pytest.mark.parametrize("reason", sorted(client.CHANGE_SET_REASONS))
@pytest.mark.parametrize("code", ["plan_rejected", "invalid_field", "private-fixture-canary"])
def test_change_set_feedback_requires_exact_rejection_code(reason, code):
    result = client.prepare_feedback(ExecutorError(code, reason=reason))
    assert result == {
        "code": code if code in client.PREPARE_ERROR_CODES else "prepare_rejected",
        "reason": reason if code == "plan_rejected" else "details_unavailable",
        "missing_sales_order_ids": [],
    }


@pytest.mark.parametrize("suffix", [":private-fixture-canary", "\nprivate-fixture-canary", ":123", " "])
def test_change_set_reason_never_carries_suffix_or_ids(suffix):
    result = client.prepare_feedback(ExecutorError(
        "plan_rejected", reason="change_set_mapping_unproven" + suffix,
    ))
    assert result == {"code": "plan_rejected", "reason": "details_unavailable",
                      "missing_sales_order_ids": []}

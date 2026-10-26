"""Socket-level tests for the executor daemon and its injected handlers."""

from __future__ import annotations

import fcntl
import json
import os
import pwd
import socket
import stat
import threading
from pathlib import Path

import pytest

from forgeflow.executor import daemon
from forgeflow.executor.daemon import ExecutorServer, Handlers
from forgeflow.executor.peer import PeerError
from forgeflow.executor.protocol import MAX_FRAME_BYTES, ProtocolError
from forgeflow.recovery import client

RUN_ID = "run-1"
PLAN_ID = "a" * 64


def envelope(op: str, **fields: object) -> dict[str, object]:
    return {"schema_version": 1, "op": op, **fields}


def allow_peer(_sock: socket.socket, _uid: int) -> None:
    return None


def ask(socket_path: Path, payload: dict[str, object]) -> dict[str, object]:
    return client.request(str(socket_path), payload, timeout=3)


def rejected(socket_path: Path, payload: dict[str, object]) -> client.ExecutorError:
    with pytest.raises(client.ExecutorError) as caught:
        ask(socket_path, payload)
    return caught.value


def raw_exchange(socket_path: Path, wire: bytes) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(3)
        sock.connect(str(socket_path))
        try:
            sock.sendall(wire)
        except OSError:
            pass
        data = bytearray()
        while b"\n" not in data:
            chunk = sock.recv(4096)
            if not chunk:
                break
            data.extend(chunk)
    return json.loads(bytes(data))


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    # A relative socket name keeps AF_UNIX addresses below the macOS limit.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(daemon, "require_peer", allow_peer)
    return tmp_path


@pytest.fixture
def handlers() -> tuple[Handlers, list[tuple[object, ...]]]:
    calls: list[tuple[object, ...]] = []

    def prepare(run_id: str, request: dict[str, object]) -> dict[str, object]:
        calls.append(("prepare", run_id, request))
        return {"result": "prepare"}

    def apply(
        run_id: str, plan_id: str, approval: dict[str, object] | None
    ) -> dict[str, object]:
        calls.append(("apply", run_id, plan_id, approval))
        return {"result": "apply"}

    def status(run_id: str, plan_id: str) -> dict[str, object]:
        calls.append(("status", run_id, plan_id))
        return {"result": "status"}

    return Handlers(prepare, apply, status), calls


@pytest.fixture
def serve(
    workspace: Path, handlers: tuple[Handlers, list[tuple[object, ...]]]
) -> object:
    active: list[tuple[ExecutorServer, threading.Event, threading.Thread]] = []

    def start(
        *,
        loop: bool = True,
        socket_name: str = "s.sock",
        ready_name: str | None = "r.json",
        custom: Handlers | None = None,
        **options: object,
    ) -> ExecutorServer:
        server = ExecutorServer(
            Path(socket_name),
            handlers[0] if custom is None else custom,
            agent_uid=int(options.pop("agent_uid", os.getuid())),
            ready_path=Path(ready_name) if ready_name is not None else None,
            **options,
        )
        server.bind()
        stop = threading.Event()
        thread = threading.Thread(target=server.serve_forever, args=(stop,), daemon=True)
        if loop:
            thread.start()
        active.append((server, stop, thread))
        return server

    yield start
    for server, stop, thread in active:
        stop.set()
        if thread.is_alive():
            thread.join(timeout=3)
        assert not thread.is_alive()
        server.close()


def test_health_returns_ok(serve) -> None:
    server = serve()
    response = ask(server.socket_path, envelope("health"))
    assert response["ok"] is True
    assert response["op"] == "health"
    assert response["schema_version"] == 1
    assert response["pid"] == os.getpid()
    assert response["socket"] == "s.sock"


def test_health_uses_injected_payload(serve, handlers) -> None:
    base, _ = handlers
    injected = Handlers(
        base.prepare, base.apply, base.status, health=lambda: {"ready": True}
    )
    server = serve(custom=injected)
    response = ask(server.socket_path, envelope("health"))
    assert (response["ok"], response["ready"]) == (True, True)


def test_product_reconcile_is_disabled_by_default_without_apply_or_status(serve, handlers):
    server = serve()
    error = rejected(server.socket_path, envelope("reconcile", run_id=RUN_ID, plan_id=PLAN_ID))
    assert error.code == "product_reconciliation_disabled"
    assert handlers[1] == []


def test_product_reconcile_client_routes_only_to_explicit_reader(serve, handlers):
    base, calls = handlers

    def reader(run, plan):
        calls.append(("reconcile", run, plan))
        return {"execution_validated": True}

    server = serve(custom=Handlers(base.prepare, base.apply, base.status, reconcile=reader))
    result = client.reconcile(str(server.socket_path), RUN_ID, PLAN_ID, timeout=3)
    assert result["execution_validated"] is True
    assert calls == [("reconcile", RUN_ID, PLAN_ID)]


def test_dispatch_parses_fields(serve, handlers) -> None:
    server = serve()
    _, calls = handlers
    approval = {"nonce": "n"}
    assert ask(
        server.socket_path,
        envelope("prepare", run_id=RUN_ID, request={"intent": "repair"}),
    )["result"] == "prepare"
    assert ask(
        server.socket_path,
        envelope("apply", run_id=RUN_ID, plan_id=PLAN_ID, approval=approval),
    )["result"] == "apply"
    assert ask(
        server.socket_path,
        envelope("apply", run_id=RUN_ID, plan_id=PLAN_ID),
    )["result"] == "apply"
    assert ask(
        server.socket_path,
        envelope("status", run_id=RUN_ID, plan_id=PLAN_ID),
    )["result"] == "status"
    assert calls == [
        ("prepare", RUN_ID, {"intent": "repair"}),
        ("apply", RUN_ID, PLAN_ID, approval),
        ("apply", RUN_ID, PLAN_ID, None),
        ("status", RUN_ID, PLAN_ID),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        envelope("prepare", run_id=RUN_ID, request={}, plan_id=PLAN_ID),
        envelope("apply", run_id=RUN_ID, plan_id=PLAN_ID, request={}),
        envelope("status", run_id=RUN_ID, plan_id=PLAN_ID, approval=None),
        envelope("health", run_id=RUN_ID),
    ],
)
def test_unknown_keys_are_rejected(serve, payload) -> None:
    server = serve()
    error = rejected(server.socket_path, payload)
    assert (error.code, error.status) == ("unknown_key", 400)


@pytest.mark.parametrize(
    "payload",
    [
        envelope("prepare", run_id=RUN_ID, request={}),
        envelope("apply", run_id=RUN_ID, plan_id=PLAN_ID),
        envelope("status", run_id=RUN_ID, plan_id=PLAN_ID),
    ],
)
def test_handler_protocol_error_envelope(serve, payload) -> None:
    def fail(*_args: object) -> dict[str, object]:
        raise ProtocolError("execution_conflict", 409, reason="precondition_conflict")

    server = serve(custom=Handlers(fail, fail, fail))
    error = rejected(server.socket_path, payload)
    assert (error.code, error.status, error.retryable, error.reason) == (
        "execution_conflict",
        409,
        False,
        "precondition_conflict",
    )


def test_handler_value_error_is_internal_and_server_survives(serve, handlers) -> None:
    base, _ = handlers
    attempts = 0

    def prepare(_run_id: str, _request: dict[str, object]) -> dict[str, object]:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise ValueError("boom")
        return {"result": "recovered"}

    server = serve(custom=Handlers(prepare, base.apply, base.status))
    error = rejected(
        server.socket_path, envelope("prepare", run_id=RUN_ID, request={})
    )
    assert (error.code, error.status) == ("internal_error", 500)
    response = ask(server.socket_path, envelope("prepare", run_id=RUN_ID, request={}))
    assert response["result"] == "recovered"


def test_peer_rejection_then_next_request_works(serve, monkeypatch) -> None:
    server = serve()

    def deny(*_args: object, **_kwargs: object) -> None:
        raise PeerError("peer_rejected")

    monkeypatch.setattr(daemon, "require_peer", deny)
    with pytest.raises(client.ExecutorError) as caught:
        ask(server.socket_path, envelope("health"))
    assert caught.value.code in {"peer_rejected", "internal_error", "unavailable"}
    monkeypatch.setattr(daemon, "require_peer", allow_peer)
    assert ask(server.socket_path, envelope("health"))["ok"] is True


def test_two_sequential_connections(serve) -> None:
    server = serve()
    assert ask(server.socket_path, envelope("health"))["ok"] is True
    assert ask(server.socket_path, envelope("health"))["ok"] is True


def test_oversize_body_is_rejected_and_server_survives(serve) -> None:
    server = serve()
    response = raw_exchange(server.socket_path, b"x" * (MAX_FRAME_BYTES + 1))
    assert (response["error"]["code"], response["error"]["status"]) == (
        "body_too_large",
        413,
    )
    assert ask(server.socket_path, envelope("health"))["ok"] is True


def test_bind_twice_is_already_running(serve, handlers) -> None:
    first = serve()
    with pytest.raises(RuntimeError, match="executor_already_running"):
        first.bind()
    second = ExecutorServer(Path("s.sock"), handlers[0], agent_uid=os.getuid())
    try:
        with pytest.raises(RuntimeError, match="executor_already_running"):
            second.bind()
    finally:
        second.close()
    assert ask(first.socket_path, envelope("health"))["ok"] is True


def test_live_socket_with_other_lock_is_already_running(serve, handlers) -> None:
    first = serve()
    second = ExecutorServer(
        Path("s.sock"),
        handlers[0],
        agent_uid=os.getuid(),
        lock_path=Path("other.lock"),
    )
    try:
        with pytest.raises(RuntimeError, match="executor_already_running"):
            second.bind()
    finally:
        second.close()
    assert ask(first.socket_path, envelope("health"))["ok"] is True


def test_stale_socket_is_replaced(workspace: Path, serve) -> None:
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind("s.sock")
    stale.close()
    assert Path("s.sock").exists()
    server = serve()
    assert ask(server.socket_path, envelope("health"))["ok"] is True


def test_non_socket_path_is_rejected(workspace: Path, handlers) -> None:
    Path("s.sock").write_text("not a socket")
    server = ExecutorServer(Path("s.sock"), handlers[0], agent_uid=os.getuid())
    try:
        with pytest.raises(RuntimeError, match="socket_path_not_socket"):
            server.bind()
    finally:
        server.close()
    assert Path("s.sock").read_text() == "not a socket"


def test_ready_file_has_pid_and_inode_and_is_removed(serve) -> None:
    server = serve()
    ready = Path("r.json")
    payload = json.loads(ready.read_text())
    assert payload["schema_version"] == 1
    assert payload["pid"] == os.getpid()
    assert payload["socket"] == "s.sock"
    assert payload["socket_ino"] == os.stat("s.sock").st_ino
    assert stat.S_IMODE(ready.stat().st_mode) == 0o644
    server.close()
    assert not ready.exists()
    assert not Path("s.sock").exists()


def test_serve_once_timeout_returns_false(serve) -> None:
    server = serve(loop=False)
    assert server.serve_once(timeout=0.05) is False


def test_stop_event_cleans_up_and_releases_lock(serve) -> None:
    server = serve(loop=False)
    stop = threading.Event()
    thread = threading.Thread(target=server.serve_forever, args=(stop,))
    thread.start()
    stop.set()
    thread.join(timeout=3)
    assert not thread.is_alive()
    assert not Path("s.sock").exists()
    assert not Path("r.json").exists()
    with open("executor.lock", "rb") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_socket_mode_and_chown_flag(serve) -> None:
    server = serve()
    info = os.stat("s.sock")
    assert stat.S_ISSOCK(info.st_mode)
    assert stat.S_IMODE(info.st_mode) == 0o660
    if os.geteuid() == 0:
        assert server.chown_skipped is False
        assert info.st_gid == pwd.getpwuid(os.getuid()).pw_gid
    else:
        assert server.chown_skipped is True


def test_strict_socket_rejects_permission_fallback(workspace, handlers, monkeypatch):
    monkeypatch.setattr(daemon.os, "geteuid", lambda: 0)
    monkeypatch.setattr(daemon.os, "chown", lambda *_: (_ for _ in ()).throw(PermissionError()))
    monkeypatch.setattr(daemon.pwd, "getpwuid", lambda _uid: type("User", (), {"pw_gid": 10001})())
    server = ExecutorServer(Path("strict.sock"), handlers[0], agent_uid=10001,
                            strict_ownership=True)
    with pytest.raises(RuntimeError, match="socket_ownership_required"):
        server.bind()
    assert not Path("strict.sock").exists()
    assert not list(Path(".").glob(".executor.*.sock"))


@pytest.mark.parametrize("uid,peer", [(10001, None), (0, allow_peer)])
def test_strict_socket_requires_root_and_real_peer_gate(workspace, handlers, monkeypatch, uid, peer):
    monkeypatch.setattr(daemon.os, "geteuid", lambda: uid)
    server = ExecutorServer(Path("strict.sock"), handlers[0], agent_uid=10001,
                            strict_ownership=True, peer_check=peer)
    with pytest.raises(RuntimeError, match="executor_requires_root_peer_gate"):
        server.bind()
    assert not Path("strict.sock").exists()


def test_bind_creates_parent_directory_0711(serve) -> None:
    server = serve(socket_name="run/s.sock", ready_name="run/r.json")
    assert stat.S_IMODE(os.stat("run").st_mode) == 0o711
    assert ask(server.socket_path, envelope("health"))["ok"] is True


def test_main_returns_two_on_bind_error(workspace: Path, monkeypatch) -> None:
    def fail_bind(_server: ExecutorServer) -> None:
        raise RuntimeError("executor_already_running")

    monkeypatch.setattr(daemon.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(ExecutorServer, "bind", fail_bind)
    agent_user = pwd.getpwuid(os.getuid()).pw_name
    assert daemon.main(
        ["--socket", "s.sock", "--agent-user", agent_user, "--ready-file", "r.json"]
    ) == 2

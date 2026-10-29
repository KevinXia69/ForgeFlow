"""Tests for the executor bundle entry point; no sockets or live Odoo."""

from __future__ import annotations

import os
import pwd
import signal
import sys
import threading
import types
from pathlib import Path

import pytest

from forgeflow.executor import main
from forgeflow.executor.daemon import ExecutorServer, Handlers

USER = pwd.getpwuid(os.getuid()).pw_name
PATH_FLAGS = ("--socket", "--store", "--bundle", "--key", "--approval-store", "--policy-file")
REQUIRED = ("--socket", "--store", "--bundle", "--key", "--agent-user")


def _args(tmp_path: Path, *, autonomous: bool = False, **flags: str) -> list[str]:
    argv = [
        "--socket", str(tmp_path / "executor.sock"), "--store", str(tmp_path / "store"),
        "--bundle", str(tmp_path / "bundle"), "--key", str(tmp_path / "key"),
        "--agent-user", USER,
    ]
    for name, value in flags.items():
        argv += [f"--{name.replace('_', '-')}", value]
    if autonomous:
        argv.append("--autonomous")
    return argv


def _config(tmp_path: Path, **overrides: object) -> main.ExecutorConfig:
    values: dict[str, object] = {
        "socket_path": tmp_path / "executor.sock", "store": tmp_path / "store",
        "bundle": tmp_path / "bundle", "key_path": tmp_path / "key", "agent_user": USER,
    }
    values.update(overrides)
    return main.ExecutorConfig(**values)


def _fake_odoolib() -> tuple[types.ModuleType, list[dict[str, object]]]:
    calls: list[dict[str, object]] = []
    module = types.ModuleType("odoolib")

    def get_connection(**kwargs: object) -> object:
        calls.append(kwargs)
        return object()

    module.get_connection = get_connection
    return module, calls


def test_config_parses_every_flag(tmp_path: Path) -> None:
    config = main.config_from_args(_args(
        tmp_path, autonomous=True, approval_store=str(tmp_path / "approvals"),
        policy_file=str(tmp_path / "policy.json"), database="odoo",
        host="odoo.internal", port="9123"))
    assert config == main.ExecutorConfig(
        socket_path=tmp_path / "executor.sock", store=tmp_path / "store",
        bundle=tmp_path / "bundle", key_path=tmp_path / "key", agent_user=USER,
        approval_store=tmp_path / "approvals", policy_file=tmp_path / "policy.json",
        database="odoo", host="odoo.internal", port=9123, require_grant=False)


def test_defaults_and_autonomous(tmp_path: Path) -> None:
    config = main.config_from_args(_args(tmp_path))
    assert (config.approval_store, config.policy_file, config.database, config.host,
            config.port, config.require_grant) == (None, None, "bench", "127.0.0.1", 8069, True)
    assert main.config_from_args(_args(tmp_path, autonomous=True)).require_grant is False


@pytest.mark.parametrize("flag", PATH_FLAGS)
def test_relative_path_is_rejected(tmp_path: Path, flag: str) -> None:
    argv = _args(tmp_path)
    if flag in argv:
        argv[argv.index(flag) + 1] = "relative/path"
    else:
        argv += [flag, "relative/path"]
    with pytest.raises(SystemExit) as caught:
        main.config_from_args(argv)
    assert caught.value.code == 2


@pytest.mark.parametrize("flag", REQUIRED)
def test_missing_required_flag_exits_two(tmp_path: Path, flag: str) -> None:
    argv = _args(tmp_path)
    index = argv.index(flag)
    del argv[index:index + 2]
    with pytest.raises(SystemExit) as caught:
        main.config_from_args(argv)
    assert caught.value.code == 2


def test_connection_factory_values(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    key_path = tmp_path / "key"
    key_path.write_text("secret\n", encoding="utf-8")
    key_path.chmod(0o400)
    module, calls = _fake_odoolib()
    monkeypatch.setitem(sys.modules, "odoolib", module)
    config = _config(
        tmp_path, key_path=key_path, host="odoo.internal", port=9123, database="prod"
    )
    assert main.connection_factory(config)() is not None
    assert calls == [{
        "hostname": "odoo.internal", "protocol": "json2", "port": 9123,
        "database": "prod", "password": "secret",
    }]


@pytest.mark.parametrize("linked", [False, True])
def test_untrusted_key_is_rejected(tmp_path: Path, linked: bool) -> None:
    target = tmp_path / "target"
    target.write_text("secret", encoding="utf-8")
    target.chmod(0o400 if linked else 0o644)
    key_path = tmp_path / "key"
    if linked:
        key_path.symlink_to(target)
    else:
        key_path = target
    with pytest.raises(RuntimeError, match="^key_not_readable$"):
        main.read_key_text(key_path)


def test_build_server_wires_handlers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    made: list[object] = []

    class Recorder:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs
            made.append(self)

        def prepare(self, *_args: object) -> dict[str, object]:
            return {}

        apply = prepare
        status = prepare
    monkeypatch.setattr(main, "RecoveryHandlers", Recorder)
    config = _config(tmp_path, approval_store=tmp_path / "approvals",
                     policy_file=tmp_path / "policy.json", require_grant=False)
    server = main.build_server(config, peer_check=lambda *_args: None)
    recovery = made[0]
    kwargs = recovery.kwargs
    assert isinstance(server, ExecutorServer)
    assert callable(kwargs["connection_factory"])
    assert {key: value for key, value in kwargs.items() if key != "connection_factory"} == {
        "store": tmp_path / "store", "bundle": tmp_path / "bundle",
        "approval_store": tmp_path / "approvals", "require_grant": False,
        "policy_file": tmp_path / "policy.json",
        "retention_profile": None,
    }
    assert server.handlers == Handlers(recovery.prepare, recovery.apply, recovery.status)
    assert server.handlers.health is None
    assert server.agent_uid == pwd.getpwnam(config.agent_user).pw_uid


def test_build_server_explicit_handlers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def noop(*_args: object) -> dict[str, object]:
        return {}

    handlers = Handlers(noop, noop, noop)

    def forbidden(**_kwargs: object) -> None:
        raise AssertionError("RecoveryHandlers must not be constructed")

    monkeypatch.setattr(main, "RecoveryHandlers", forbidden)
    assert main.build_server(_config(tmp_path), handlers=handlers).handlers is handlers


class FakeServer:
    def __init__(self, fail_bind: bool = False) -> None:
        self.calls: list[str] = []
        self.fail_bind = fail_bind

    def bind(self) -> None:
        self.calls.append("bind")
        if self.fail_bind:
            raise RuntimeError("executor_already_running")

    def serve_forever(self, stop: threading.Event) -> None:
        self.calls.append("serve_forever")
        stop.set()

    def close(self) -> None:
        self.calls.append("close")


def test_run_binds_serves_closes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    server = FakeServer()
    installed: dict[int, object] = {}
    def fake_signal(signum: int, handler: object) -> None:
        installed[signum] = handler

    monkeypatch.setattr(main, "build_server", lambda _config, **_kwargs: server)
    monkeypatch.setattr(main.signal, "signal", fake_signal)
    stop = threading.Event()
    def serve(stop_event: threading.Event) -> None:
        server.calls.append("serve")
        assert set(installed) >= {signal.SIGTERM, signal.SIGINT}
        installed[signal.SIGTERM]()
        assert stop_event.is_set()

    assert main.run(_args(tmp_path), stop_event=stop, serve=serve) == 0
    assert server.calls == ["bind", "serve", "close"]


def test_run_bind_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                        capsys: pytest.CaptureFixture[str]) -> None:
    server = FakeServer(fail_bind=True)
    monkeypatch.setattr(main, "build_server", lambda _config, **_kwargs: server)
    monkeypatch.setattr(main.signal, "signal", lambda *_args: None)
    def serve(_stop: threading.Event) -> None:
        pytest.fail("serve must not run after a bind failure")

    assert main.run(_args(tmp_path), serve=serve) == 2
    error = capsys.readouterr().err
    assert len(error.strip().splitlines()) == 1

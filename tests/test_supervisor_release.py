from __future__ import annotations

import importlib.util
import io
import json
import signal
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location(
    "supervisor_release_test", Path(__file__).resolve().parents[1]
    / "scripts/supervisor_release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def test_only_python_package_and_declared_entrypoints_are_allowed():
    assert release.allowed("src/forgeflow/integrations/odoo20/supplier_prices.py")
    assert release.allowed("scripts/run_supervisor.py")
    assert release.allowed("scripts/forgeflow_runtime.py")
    assert release.allowed("scripts/assistant_mcp.py")
    for path in (".env", "runtime.env", "src/forgeflow/api/static/a.py",
                 "runs/a.py", "scripts/other.py", "src/forgeflow/../secret.py",
                 "src/forgeflow/api/static/index.html", "/src/forgeflow/evil.py"):
        assert not release.allowed(path)


def test_archive_hash_and_path_checks_precede_install(tmp_path):
    archive = tmp_path / "payload.tar"
    manifest = {"schema_version": 1, "tag": "v3.8.5", "commit": "a" * 40,
                "files": {name: "b" * 64 for name in release.ENTRYPOINTS}}
    with tarfile.open(archive, "w") as target:
        for name, raw in [("manifest.json", json.dumps(manifest).encode()),
                          *[(name, b"print('valid syntax')\n") for name in release.ENTRYPOINTS]]:
            entry = tarfile.TarInfo(name)
            entry.size = len(raw)
            target.addfile(entry, io.BytesIO(raw))
    stage = tmp_path / "stage"
    stage.mkdir()
    with pytest.raises(release.ReleaseError, match="release_hash_mismatch"):
        release.unpack(archive, stage)


@pytest.mark.parametrize("existing", ["src/forgeflow/old.py", "scripts/assistant_mcp.py"])
def test_rollback_restores_old_bytes_removes_only_new_files_and_preserves_data(tmp_path, existing):
    root, backup = tmp_path / "app", tmp_path / "backup"
    source = root / existing
    source.parent.mkdir(parents=True)
    source.write_text("old = True\n")
    source.chmod(0o640)
    added = root / "src/forgeflow/new.py"
    added.parent.mkdir(parents=True, exist_ok=True)
    protected = root / "runtime.env"
    protected.write_text("private configuration\n")
    manifest = {"files": {existing: "unused", "src/forgeflow/new.py": "unused"}}
    records = release.backup_files(root, backup, manifest)
    source.write_text("new = True\n")
    added.write_text("added = True\n")
    records[existing]["new_sha256"] = release.digest(source)
    records["src/forgeflow/new.py"]["new_sha256"] = release.digest(added)
    release.restore_files(root, backup, records)
    assert source.read_text() == "old = True\n"
    assert source.stat().st_mode & 0o777 == 0o640
    assert not added.exists()
    assert protected.read_text() == "private configuration\n"


def test_rollback_refuses_to_overwrite_a_later_manual_change(tmp_path):
    root, backup = tmp_path / "app", tmp_path / "backup"
    source = root / "scripts/run_supervisor.py"
    source.parent.mkdir(parents=True)
    source.write_text("old = True\n")
    records = release.backup_files(root, backup, {"files": {"scripts/run_supervisor.py": "b" * 64}})
    source.write_text("manual = True\n")
    with pytest.raises(release.ReleaseError, match="rollback_source_changed"):
        release.restore_files(root, backup, records)
    assert source.read_text() == "manual = True\n"


def stop_fixture(monkeypatch, live_after_term=False):
    calls = []
    identity = {"pid": 1234, "start_ticks": "100", "state": "S"}
    def send(identity, command, cwd, sig):
        assert sig != signal.SIGTERM, "TERM must target the Python main thread"
        calls.append(sig)
    def current(*args):
        if signal.SIGTERM in calls and not live_after_term:
            return None
        return {**identity, "state": "T" if signal.SIGSTOP in calls else "S"}
    monkeypatch.setattr(release, "signal_group", send)
    monkeypatch.setattr(release, "signal_main_thread", lambda *args: calls.append(args[-1]))
    monkeypatch.setattr(release, "same_process", current)
    return identity, calls


def test_busy_work_refuses_before_any_signal(monkeypatch):
    identity, calls = stop_fixture(monkeypatch)
    def busy(_):
        raise release.ReleaseError("supervisor_business_in_flight")
    with pytest.raises(release.ReleaseError, match="business_in_flight"):
        release.stop_idle(identity, [], Path("/app"), busy)
    assert calls == []


def test_work_claimed_in_the_pause_window_is_resumed_without_termination(monkeypatch):
    identity, calls = stop_fixture(monkeypatch)
    def became_busy(second):
        if second:
            raise release.ReleaseError("control_transaction_in_flight")
    with pytest.raises(release.ReleaseError, match="transaction_in_flight"):
        release.stop_idle(identity, [], Path("/app"), became_busy)
    assert calls == [signal.SIGSTOP, signal.SIGCONT]


def test_idle_shutdown_uses_term_and_cont_without_force(monkeypatch):
    identity, calls = stop_fixture(monkeypatch)
    checks = []
    release.stop_idle(identity, [], Path("/app"), lambda second: checks.append(second))
    assert checks == [False, True]
    assert calls == [signal.SIGSTOP, signal.SIGTERM, signal.SIGCONT]


def test_stop_timeout_never_force_kills(monkeypatch):
    identity, calls = stop_fixture(monkeypatch, live_after_term=True)
    with pytest.raises(release.ReleaseError, match="stop_timeout_old_sources_retained"):
        release.stop_idle(identity, [], Path("/app"), lambda _: None, timeout=0)
    assert signal.SIGKILL not in calls


def test_pid_reuse_and_zero_pid_are_rejected(monkeypatch):
    with pytest.raises(release.ReleaseError, match="supervisor_pid_invalid"):
        release.process_identity(0, [], Path("/app"))
    monkeypatch.setattr(release, "process_identity", lambda *_: {"start_ticks": "200"})
    with pytest.raises(release.ReleaseError, match="supervisor_pid_reused"):
        release.same_process({"pid": 1234, "start_ticks": "100"}, [], Path("/app"))


def test_initial_deploy_requires_a_real_recorded_process(tmp_path, monkeypatch):
    pid_path = tmp_path / "supervisor.pid"
    pid_path.write_text("1234")
    monkeypatch.setattr(release, "process_identity", lambda *_: None)
    with pytest.raises(release.ReleaseError, match="supervisor_not_running"):
        release.stop_recorded([], tmp_path, pid_path, object(), must_be_running=True)


def test_heartbeat_gate_requires_all_four_since_database_start_time():
    since = datetime(2026, 11, 10, tzinfo=UTC)
    observed = []
    def execute(query, params):
        observed.append((query, params))
        return SimpleNamespace(fetchall=lambda: [(name,) for name in params[0][:-1]])
    activity = release.Activity.__new__(release.Activity)
    activity.connection = SimpleNamespace(execute=execute)
    assert not activity.heartbeats("tenant", since)
    query, params = observed[0]
    assert "last_seen >= %s" in query
    assert params == (["source:tenant", "dispatcher:dispatcher-supervised", "watchdog",
                       "outbox:tenant"], since)

"""Temporary state and owned Python processes only; no ERP/model/runtime reads."""
from __future__ import annotations

import os
import signal
import subprocess
import sys

import pytest

from forgeflow import runtime_lifecycle as lifecycle


def identity(pid=12345, *, start="synthetic:1", uid=None, ppid=1):
    return {"pid": pid, "uid": os.getuid() if uid is None else uid, "start": start,
            "ppid": ppid, "pgid": pid}


def record(path, *, root=None, children=None):
    value = {"schema_version": 1, "identity": root or identity(),
             "children": children or [], "role": "synthetic"}
    lifecycle.write_record(path, value)
    return value


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public", "corrupt", "directory"])
def test_untrusted_record_is_not_read_or_replaced(tmp_path, kind):
    path, target = tmp_path / "pid", tmp_path / "retained"
    lifecycle.write_record(target, {"retained": True})
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    elif kind == "directory":
        path.mkdir()
    else:
        path.write_text("bad" if kind == "corrupt" else "{}")
        path.chmod(0o600 if kind == "corrupt" else 0o644)
    original = target.read_bytes()
    with pytest.raises(lifecycle.LifecycleError):
        lifecycle.write_record(path, {"replacement": True})
    assert target.read_bytes() == original


def test_lease_nonblocking_exclusion_release_and_readonly_missing(tmp_path):
    lock = tmp_path / "scope" / "operation.lock"
    with lifecycle.exclusive_lease(lock, create=False):
        assert not lock.parent.exists()
    with lifecycle.exclusive_lease(lock):
        with pytest.raises(lifecycle.LifecycleError, match="operation_in_progress"):
            with lifecycle.exclusive_lease(lock):
                pytest.fail("concurrent lease acquired")
    with lifecycle.exclusive_lease(lock):
        assert lock.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("kind", ["symlink", "hardlink", "public"])
def test_lease_rejects_unsafe_lock(tmp_path, kind):
    path, target = tmp_path / "lock", tmp_path / "target"
    target.touch(mode=0o600)
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "hardlink":
        os.link(target, path)
    else:
        path.touch(mode=0o644)
    with pytest.raises(lifecycle.LifecycleError, match="unsafe_lease"):
        with lifecycle.exclusive_lease(path):
            pytest.fail("unsafe lock acquired")


def test_pid_reuse_refuses_every_signal_and_keeps_record(tmp_path, monkeypatch):
    path = tmp_path / "pid"
    record(path)
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: identity(pid, start="new:2"))
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("reused PID signalled"))
    with pytest.raises(lifecycle.LifecycleError, match="pid_reused_refusing_signal"):
        lifecycle.stop_process(path)
    assert path.exists()


def test_reused_parent_during_capture_refuses_child_signal(tmp_path, monkeypatch):
    path = tmp_path / "pid"
    record(path)
    values = iter([identity(), identity(start="new:2")])
    monkeypatch.setattr(lifecycle, "process_identity", lambda _pid: next(values))
    monkeypatch.setattr(lifecycle, "descendants", lambda _pid: [identity(23456)])
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("unbound child signalled"))
    with pytest.raises(lifecycle.LifecycleError, match="parent_identity_changed"):
        lifecycle.stop_process(path)


def test_stopped_parent_does_not_kill_reused_descendant(tmp_path, monkeypatch):
    path = tmp_path / "pid"
    record(path, children=[identity(23456)])
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: (
        None if pid == 12345 else identity(pid, start="new:2")))
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("reused child signalled"))
    lifecycle.stop_process(path, timeout=0)
    assert not path.exists()


def test_child_capture_is_durable_before_first_signal(tmp_path, monkeypatch):
    path = tmp_path / "pid"
    record(path)
    live = {12345: identity(), 23456: identity(23456, ppid=12345)}
    monkeypatch.setattr(lifecycle, "process_identity", lambda pid: live.get(pid))
    monkeypatch.setattr(lifecycle, "descendants", lambda _pid: [live[23456]])
    signals = []

    def kill(pid, sig):
        assert lifecycle.read_record(path)["children"][0]["pid"] == 23456
        signals.append((pid, sig))
        live.pop(pid)

    monkeypatch.setattr(os, "kill", kill)
    lifecycle.stop_process(path, timeout=0)
    assert signals == [(12345, signal.SIGTERM), (23456, signal.SIGTERM)]
    assert not path.exists()


def test_invalid_child_identity_is_refused_before_signalling(tmp_path, monkeypatch):
    path = tmp_path / "pid"
    record(path, children=[identity(1)])
    monkeypatch.setattr(lifecycle, "process_identity", lambda _pid: None)
    monkeypatch.setattr(os, "kill", lambda *_: pytest.fail("invalid child signalled"))
    with pytest.raises(lifecycle.LifecycleError, match="invalid_child_identity"):
        lifecycle.stop_process(path)


def test_kernel_identity_and_owned_python_process_stop(tmp_path):
    own = lifecycle.process_identity(os.getpid())
    assert own["uid"] == os.getuid()
    assert lifecycle.same_process(own, lifecycle.process_identity(os.getpid()))
    assert "args" not in own and "environ" not in own
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"],
                               start_new_session=True, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL)
    path = tmp_path / "pid"
    try:
        lifecycle.record_process(path, process.pid, "controlled-python")
        assert lifecycle.managed_process(path)[1] == "running"
        lifecycle.stop_process(path, timeout=1)
        process.wait(timeout=3)
        assert not path.exists()
        lifecycle.stop_process(path)  # Idempotent, no bare PID signalling.
    finally:
        if process.poll() is None:
            process.kill()
        process.wait()


def test_missing_resource_measurements_remain_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(lifecycle.sys, "platform", "unsupported-test")

    def missing(_path):
        raise OSError("synthetic unavailable")

    monkeypatch.setattr(lifecycle.shutil, "disk_usage", missing)
    result = lifecycle.resource_snapshot(tmp_path)
    assert result["disk_free_bytes"] is None
    assert result["available_memory_bytes"] is None
    assert result["swap_used_bytes"] is None


def test_spawn_binds_identity_before_service_runs(tmp_path):
    path, marker = tmp_path / 'pid', tmp_path / 'service-started'
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('started'); import time; time.sleep(30)"
    process = lifecycle.spawn_managed(path, [sys.executable, '-c', code], 'controlled-service',
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        assert lifecycle.managed_process(path)[1] == 'running'
        assert lifecycle.read_record(path)['identity']['pid'] == process.pid
        assert lifecycle.read_record(path)['identity']['pgid'] == process.pid
    finally:
        lifecycle.stop_process(path, timeout=1)
        process.wait(timeout=3)


def test_spawn_record_failure_prevents_service_dispatch(tmp_path, monkeypatch):
    path, marker = tmp_path / 'pid', tmp_path / 'must-not-exist'
    def reject(*args):
        raise lifecycle.LifecycleError('synthetic_record_failure')
    monkeypatch.setattr(lifecycle, 'record_process', reject)
    code = f"from pathlib import Path; Path({str(marker)!r}).write_text('unexpected service')"
    with pytest.raises(lifecycle.LifecycleError, match='synthetic_record_failure'):
        lifecycle.spawn_managed(path, [sys.executable, '-c', code], 'controlled-service',
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    assert not marker.exists()

import asyncio
import json
import os
import runpy
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow import research_containers as rc

CID = "a" * 64
OTHER = "b" * 64


class Docker:
    def __init__(self):
        self.ids = []
        self.running = True
        self.paused = False
        self.engine = "test-engine"
        self.calls = []
        self.foreign = False
        self.fail_stop = False
        self.after_ids = None

    def call(self, command, environment, **kwargs):
        self.calls.append(command)
        if command[0] == "info":
            return self.engine
        if command[0] == "ps":
            return "\n".join(self.ids)
        if command[0] == "inspect":
            return " ".join(json.dumps(value) for value in [
                command[-1], self.running, self.paused,
                "foreign" if self.foreign else "test-trial", self.directory,
            ])
        if command[0] == "unpause":
            self.paused = False
            return ""
        if command[0] == "stop":
            if self.fail_stop:
                raise TimeoutError("PRIVATE_CANARY")
            self.running = False
            if self.after_ids is not None:
                self.ids = self.after_ids
            return ""
        raise AssertionError(command)


@pytest.fixture
def registered(tmp_path, monkeypatch):
    root = tmp_path.resolve()
    directory = root / "task/task-1/environment"
    directory.mkdir(parents=True)
    docker = Docker()
    docker.directory = str(directory)
    monkeypatch.setattr(rc, "_command", docker.call)
    trial = root / "harbor/job/trial"
    rc.register_environment(trial, directory, "test-trial", {})
    docker.calls.clear()
    return root, trial, docker


def test_provider_secrets_are_not_passed_to_docker(monkeypatch):
    observed = {}

    def run(command, **kwargs):
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(rc.subprocess, "run", run)
    rc._command(["info"], {"PATH": "/bin", "DOCKER_HOST": "unix:///test",
                          "NEBIUS_API_KEY": "PRIVATE_CANARY", "OPENAI_API_KEY": "canary"})
    assert observed["env"] == {"PATH": "/bin", "DOCKER_HOST": "unix:///test"}
    assert observed["timeout"] == 8


def test_stop_only_owned_containers_without_delete(registered):
    root, trial, docker = registered
    docker.ids = [CID]
    report = rc.quiesce_run(root, {})
    assert report["quiescent"] and report["status"] == "stopped"
    assert report["containers"] == [{"id": CID, "running": False, "paused": False}]
    assert ["stop", "--time", "5", CID] in docker.calls
    assert not any(call[0] in {"rm", "prune", "down"} for call in docker.calls)
    assert not report["erp_rollback_confirmed"]
    assert not report["provider_cancellation_confirmed"]
    assert (trial / rc.LEDGER).stat().st_mode & 0o777 == 0o600


def test_paused_owned_container_is_resumed_only_for_stop(registered):
    root, _, docker = registered
    docker.ids, docker.paused = [CID], True
    assert rc.quiesce_run(root, {})["quiescent"]
    assert docker.calls.index(["unpause", CID]) < docker.calls.index(["stop", "--time", "5", CID])


@pytest.mark.parametrize("failure", ["engine", "foreign", "timeout", "new_container", "invalid_id"])
def test_unknown_cleanup_never_claims_success_or_leaks_diagnostics(registered, failure):
    root, _, docker = registered
    docker.ids = [CID]
    if failure == "engine":
        docker.engine = "other-engine"
    elif failure == "foreign":
        docker.foreign = True
    elif failure == "timeout":
        docker.fail_stop = True
    elif failure == "new_container":
        docker.after_ids = [OTHER]
    else:
        docker.ids = ["--all"]
    report = rc.quiesce_run(root, {})
    assert not report["quiescent"] and report["status"] == "unknown"
    assert "PRIVATE_CANARY" not in json.dumps(report)
    if failure in {"engine", "foreign", "invalid_id"}:
        assert not any(call[0] in {"stop", "unpause"} for call in docker.calls)


def test_completed_harbor_removal_and_before_registration_failure(registered, tmp_path):
    root, _, _ = registered
    assert rc.quiesce_run(root, {})["status"] == "absent"
    other = tmp_path / "empty"
    other.mkdir()
    assert rc.quiesce_run(other, {})["status"] == "not_registered"


@pytest.mark.parametrize("failure", ["directory", "ledger_link", "parent_link", "preexisting", "duplicate"])
def test_invalid_or_ambiguous_ownership_refuses_any_mutation(registered, failure):
    root, trial, docker = registered
    path = trial / rc.LEDGER
    record = json.loads(path.read_text())
    if failure == "directory":
        record["directory"] = str(root.parent)
        path.write_text(json.dumps(record))
    elif failure == "preexisting":
        record["preexisting_ids"] = [CID]
        path.write_text(json.dumps(record))
    elif failure == "duplicate":
        other = trial.with_name("trial-2")
        other.mkdir()
        (other / rc.LEDGER).write_text(json.dumps(record))
    elif failure == "ledger_link":
        other = root / "outside"
        path.rename(other)
        path.symlink_to(other)
    else:
        other = root / "outside-trial"
        trial.rename(other)
        trial.symlink_to(other, target_is_directory=True)
    assert not rc.quiesce_run(root, {})["quiescent"]
    assert docker.calls == []


def test_registration_prevents_harbor_start_on_existing_project(registered, monkeypatch):
    from forgeflow.environment import DockerEnvironment, GuardedDockerEnvironment

    root, _, docker = registered
    docker.ids = [CID]
    started = []

    async def start(self, force_build):
        started.append(True)

    monkeypatch.setattr(DockerEnvironment, "start", start)
    environment = object.__new__(GuardedDockerEnvironment)
    environment.trial_paths = SimpleNamespace(trial_dir=root / "harbor/job/new-trial")
    environment.environment_dir = root / "task/task-1/environment"
    environment.session_id = "test-trial"
    with pytest.raises(ValueError, match="preexisting"):
        asyncio.run(environment.start(force_build=False))
    assert not started


def test_registration_is_durable_before_underlying_environment_starts(registered, monkeypatch):
    from forgeflow.environment import DockerEnvironment, GuardedDockerEnvironment

    root, _, _ = registered
    trial = root / "harbor/job/new-trial"

    async def start(self, force_build):
        assert (trial / rc.LEDGER).is_file()
        return "underlying-start"

    monkeypatch.setattr(DockerEnvironment, "start", start)
    environment = object.__new__(GuardedDockerEnvironment)
    environment.trial_paths = SimpleNamespace(trial_dir=trial)
    environment.environment_dir = root / "task/task-1/environment"
    environment.session_id = "test-trial"
    assert asyncio.run(environment.start(force_build=False)) == "underlying-start"


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process groups")
def test_exit_of_leader_does_not_leave_term_ignoring_child(tmp_path):
    from forgeflow.runner import _stop_process_group
    from forgeflow.runtime_lifecycle import spawn_managed

    ready = tmp_path / "ready"
    script = (
        "import os,signal,time,pathlib,sys; pid=os.fork(); "
        "signal.signal(signal.SIGTERM,signal.SIG_IGN) if pid==0 else None; "
        "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())) if pid==0 else None; "
        "time.sleep(60)"
    )
    process = spawn_managed(tmp_path / "host-process.json",
                            [sys.executable, "-c", script, str(ready)], "controlled-test",
                            stdout=subprocess.PIPE)
    try:
        end = time.monotonic() + 5
        while not ready.exists():
            assert time.monotonic() < end
            time.sleep(0.02)
        assert not rc.host_group_quiescent(process.pid)
        assert _stop_process_group(process) == []
        assert rc.wait_host_group(process.pid)
        process.communicate(timeout=2)
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait(timeout=2)
        process.stdout.close()


def test_unconfirmed_cleanup_blocks_next_run_until_reconciled(tmp_path, monkeypatch):
    from test_runner import make_fake_workspace

    from forgeflow import runner

    make_fake_workspace(tmp_path)
    marker = tmp_path / ".runtime/lifecycle/research-residue.json"
    marker.parent.mkdir(parents=True)
    marker.write_text(json.dumps({"schema": 1, "mechanism": rc.MECHANISM,
                                  "run_dir": str(tmp_path / "runs/earlier")}))
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *a, **k: pytest.fail("must not launch"))
    result = runner.run_task(tmp_path, "A", "nop", run_id="blocked-by-residue")
    manifest = json.loads((result / "manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert "unconfirmed cleanup" in manifest["error"]["message"]
    assert marker.exists()


def test_cleanup_reconciliation_does_not_replay_or_rewrite_failed_run(
    tmp_path, monkeypatch, synthetic_host_runtime,
):
    from test_runner import make_fake_workspace

    from forgeflow import runner

    make_fake_workspace(tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=test-nebius\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(rc, "quiesce_run", lambda *a: {"status": "unknown", "quiescent": False})
    run = runner.run_task(tmp_path, "A", "pi", run_id="cleanup-unknown")
    before = (run / "manifest.json").read_bytes()
    assert json.loads(before)["state"] == "failed"
    marker = tmp_path / ".runtime/lifecycle/research-residue.json"
    assert marker.exists()
    module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/reconcile_research_cleanup.py"))
    function = module["reconcile"]
    monkeypatch.setitem(function.__globals__, "quiesce_run", lambda *a: {"status": "absent", "quiescent": True})
    assert function(tmp_path)["quiescent"]
    assert not marker.exists()
    assert (run / "manifest.json").read_bytes() == before
    assert (run / "host-cleanup-reconciliation.json").exists()


@pytest.mark.parametrize("status", ["running", "pid_reused", "not_recorded", "live_children"])
def test_reconciliation_requires_confirmed_host_exit(tmp_path, monkeypatch, status):
    from forgeflow.runtime_lifecycle import write_record

    state = tmp_path / ".runtime/lifecycle"
    state.mkdir(parents=True)
    run = tmp_path / "runs/prior"
    run.mkdir(parents=True)
    write_record(state / "research-residue.json", {"schema": 1, "mechanism": rc.MECHANISM,
                                                  "run_dir": str(run)})
    function = runpy.run_path(str(Path(__file__).resolve().parents[1] / "scripts/reconcile_research_cleanup.py"))["reconcile"]
    process = None if status == "not_recorded" else {"identity": {"pid": 1234}}
    observed = "exited" if status == "live_children" else status
    monkeypatch.setitem(function.__globals__, "managed_process", lambda p: (process, observed))
    monkeypatch.setitem(function.__globals__, "wait_host_group", lambda pid: False)
    monkeypatch.setitem(function.__globals__, "quiesce_run", lambda *a: pytest.fail("must not touch Docker"))
    with pytest.raises(ValueError):
        function(tmp_path)
    assert (state / "research-residue.json").exists()


def test_pid_reuse_refuses_group_signal_before_cleanup(monkeypatch):
    from forgeflow import runner, runtime_lifecycle

    original = {"pid": 12345, "uid": os.getuid(), "start": "original"}
    process = SimpleNamespace(pid=12345, _forgeflow_identity=original)
    monkeypatch.setattr(runtime_lifecycle, "process_identity", lambda pid: {**original, "start": "reused"})
    monkeypatch.setattr(runner.os, "killpg", lambda *args: pytest.fail("must not signal reused PID"))
    errors = runner._stop_process_group(process)
    assert errors == ["Could not signal Harbor process group: ValueError"]

import json
import os
import subprocess

import pytest

pytest.importorskip("pwd")

from forgeflow import vps_runtime  # noqa: E402
from forgeflow.dsh_launcher import launch_arguments, scoped_file  # noqa: E402
from forgeflow.vps_runtime import (  # noqa: E402
    api_environment, assistant_config_path, configuration, executor_command, lifecycle_root,
)


def config():
    return configuration({
        "repo_root": "/opt/forgeflow", "python": "/opt/forgeflow/.venv/bin/python",
        "sandbox_root": "/var/lib/forgeflow/sandbox", "case_root": "/var/lib/forgeflow/api/cases",
        "data_root": "/var/lib/forgeflow/api/runs", "socket": "/run/forgeflow/executor.sock",
        "container": "forgeflow-odoo", "scenario": "A", "odoo_port": 18069, "api_port": 8000,
    })


def test_api_environment_cannot_inherit_erp_credentials_or_docker_access():
    env = api_environment(config(), {"ERP_API_KEY": "secret", "DOCKER_HOST": "unix:///private",
        "PYTHONPATH": "/untrusted", "FORGEFLOW_SESSION_SECRET": "identity",
        "FORGEFLOW_MODEL_API_KEY": "model-key", "FORGEFLOW_MODEL_NAME": "model"})
    assert not {"ERP_API_KEY", "DOCKER_HOST", "PYTHONPATH"} & env.keys()
    assert env["FORGEFLOW_SESSION_SECRET"] == "identity"
    assert env["NEBIUS_API_KEY"] == "model-key"
    assert env["FORGEFLOW_LOCAL_ARTIFACT_RPC"] == "true"
    assert env["FORGEFLOW_DEPLOYMENT"] == "hosted"
    assert env["FORGEFLOW_LOCAL_REQUEST"].startswith("/run/forgeflow/api-config/")
    assert env["FORGEFLOW_PUBLIC_RUNS"] == ""


def test_executor_uses_api_identity_and_never_disables_the_peer_gate():
    command = executor_command(config())
    assert command[command.index("--agent-user") + 1] == "forgeflow-api"
    assert "--allow-local-peer" not in command
    assert command[command.index("--key") + 1] == "/var/lib/forgeflow/sandbox/api_key"


@pytest.fixture
def assistant_scope(tmp_path, monkeypatch):
    evidence = tmp_path / "scope.json"
    evidence.write_text("{}")
    patch = evidence.with_suffix(".patch.json")
    patch.write_text("[]")
    patch.chmod(0o640)
    monkeypatch.setattr(os, "geteuid", lambda: 123456)
    return {"node": "/usr/bin/node", "cli": "/opt/dsh/bin.js", "evidence_root": str(tmp_path)}, evidence


def test_dsh_launcher_accepts_only_scoped_sdk_or_headless(assistant_scope):
    settings, evidence = assistant_scope
    arguments = [settings["cli"], "--profile", "sdk", "--patch", str(evidence.with_suffix(".patch.json"))]
    assert launch_arguments(arguments, settings, evidence) == ["/usr/bin/node", *arguments]
    arguments[2] = "headless"
    arguments += ["--json", "--", "--patch=/user-injected"]
    assert launch_arguments(arguments, settings, evidence)[-2:] == ["--", "--patch=/user-injected"]


@pytest.mark.parametrize("arguments", [
    ["/arbitrary/script.js", "--profile", "sdk"],
    ["/opt/dsh/bin.js", "plugin", "install", "evil"],
    ["/opt/dsh/bin.js", "--profile", "sdk", "--patch", "/etc/passwd"],
    ["/opt/dsh/bin.js", "--profile", "sdk", "--eval", "process.env"],
])
def test_dsh_launcher_rejects_unreviewed_entrypoints(assistant_scope, arguments):
    settings, evidence = assistant_scope
    with pytest.raises(ValueError):
        launch_arguments(arguments, settings, evidence)


def test_supervisor_refuses_start_while_maintenance_holds_global_lock(tmp_path, monkeypatch):
    import fcntl
    from types import SimpleNamespace

    settings = config()
    settings["repo_root"] = str(tmp_path)
    lock_root = tmp_path / ".runtime/lifecycle"
    lock_root.mkdir(parents=True)
    lock_path = lock_root / "workbench.lock"
    monkeypatch.setattr(vps_runtime, "trusted_json", lambda _path: settings)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *_args: None)
    actual_fstat = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=actual_fstat(fd).st_mode, st_uid=0))
    monkeypatch.setattr(vps_runtime, "_run_locked", lambda *_args: pytest.fail("must not provision"))
    with lock_path.open("w") as lock:
        lock_path.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        with pytest.raises(ValueError, match="runtime_or_maintenance_already_active"):
            vps_runtime.run(tmp_path / "unused-config.json")


def test_container_monitor_binds_boot_identity_with_bounded_inspection(monkeypatch):
    row = {"Id": "container-1", "RestartCount": 0,
           "State": {"Running": True, "StartedAt": "boot-1"},
           "HostConfig": {"RestartPolicy": {"Name": "no"}}}
    calls = []

    def inspect(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 0, json.dumps([row]))

    monkeypatch.setattr(vps_runtime.subprocess, "run", inspect)
    assert vps_runtime.container_boot("odoo") == ("container-1", "boot-1", 0)
    assert calls[0][1]["timeout"] == 5
    assert "FORGEFLOW_SESSION_SECRET" not in calls[0][1]["env"]
    row["State"]["Running"] = False
    with pytest.raises(ValueError, match="runtime_container_unavailable"):
        vps_runtime.container_boot("odoo")


def test_container_monitor_rejects_docker_timeout(monkeypatch):
    def inspect(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("docker", 5)

    monkeypatch.setattr(vps_runtime.subprocess, "run", inspect)
    with pytest.raises(ValueError, match="runtime_container_unavailable"):
        vps_runtime.container_boot("odoo")


def isolated_config(name):
    return configuration({**config(), "instance_name": name,
        "api_user": "forgeflow-api-" + name, "dsh_user": "forgeflow-dsh-" + name,
        "dsh_root": "/var/lib/forgeflow/" + name + "/assistant",
        "dsh_patch": "/etc/forgeflow/" + name + "/dsh.patch.json",
        "dsh_config": "/etc/forgeflow/assistants/forgeflow-dsh-" + name + ".json",
        "dsh_launcher": "/usr/local/bin/forgeflow-dsh-" + name,
        "service_unit": "forgeflow-runtime@" + name + ".service"})


def test_judge_instances_keep_runtime_locks_and_privileged_peer_identities_distinct():
    first, second = isolated_config("judge1"), isolated_config("judge2")
    assert lifecycle_root(first) != lifecycle_root(second)
    assert lifecycle_root(first).as_posix().endswith("/instances/judge1")
    assert lifecycle_root(config()).as_posix().endswith("/.runtime/lifecycle")
    for value in (first, second):
        command = executor_command(value)
        assert command[command.index("--agent-user") + 1] == value["api_user"]
        assert api_environment(value, {})["FORGEFLOW_DSH_NODE"] == value["dsh_launcher"]


def test_new_instances_cannot_silently_share_legacy_assistant_identity_or_config():
    incomplete = {**config(), "instance_name": "judge1"}
    with pytest.raises(ValueError, match="isolated_runtime_identities_required"):
        configuration(incomplete)
    wrong = isolated_config("judge1") | {"dsh_config": "/etc/forgeflow/dsh.json"}
    with pytest.raises(ValueError, match="assistant_configuration_identity_mismatch"):
        configuration(wrong)
    same_user = isolated_config("judge1") | {"api_user": "forgeflow-dsh-judge1"}
    with pytest.raises(ValueError, match="isolated_runtime_identities_required"):
        configuration(same_user)


@pytest.mark.parametrize("name", ["../judge1", "judge1/../../default", "-x", "", "judge 1"])
def test_instance_names_cannot_escape_lock_namespace(name):
    with pytest.raises(ValueError, match="invalid_runtime_instance"):
        configuration({**config(), "instance_name": name})


def test_assistant_config_is_selected_only_from_effective_unix_identity():
    assert str(assistant_config_path("forgeflow-dsh")) == "/etc/forgeflow/dsh.json"
    assert str(assistant_config_path("forgeflow-dsh-judge2")) == "/etc/forgeflow/assistants/forgeflow-dsh-judge2.json"
    for name in ("root", "forgeflow-api-judge1", "forgeflow-dsh-../judge2"):
        with pytest.raises(ValueError, match="invalid_assistant_identity"):
            assistant_config_path(name)


def test_assistant_rejects_evidence_owned_by_a_different_api_identity(assistant_scope):
    settings, evidence = assistant_scope
    with pytest.raises(ValueError, match="invalid_assistant_scope"):
        scoped_file(evidence, evidence.parent, owner_uid=evidence.stat().st_uid + 1)


def test_another_judge_runtime_is_not_blocked_by_first_judge_maintenance(tmp_path, monkeypatch):
    import fcntl
    from types import SimpleNamespace

    first, second = isolated_config("judge1"), isolated_config("judge2")
    first["repo_root"] = second["repo_root"] = str(tmp_path)
    first_lock = lifecycle_root(first) / "workbench.lock"
    first_lock.parent.mkdir(parents=True)
    monkeypatch.setattr(vps_runtime, "trusted_json", lambda _path: second)
    monkeypatch.setattr(os, "geteuid", lambda: 0)
    monkeypatch.setattr(os, "chown", lambda *_args: None)
    actual_fstat = os.fstat
    monkeypatch.setattr(os, "fstat", lambda fd: SimpleNamespace(st_mode=actual_fstat(fd).st_mode, st_uid=0))
    monkeypatch.setattr(vps_runtime, "_run_locked", lambda *_args: 17)
    with first_lock.open("w") as lock:
        first_lock.chmod(0o600)
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert vps_runtime.run(tmp_path / "unused-config.json") == 17

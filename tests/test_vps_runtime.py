import json
import os
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("pwd")

from forgeflow import vps_runtime  # noqa: E402
from forgeflow.dsh_launcher import (  # noqa: E402
    assistant_key_env,
    assistant_launch_environment,
    launch_arguments,
    scoped_file,
)
from forgeflow.product.local import candidate_profile_setting  # noqa: E402
from forgeflow.vps_runtime import (  # noqa: E402
    api_environment,
    assistant_binding,
    assistant_config_path,
    configuration,
    executor_command,
    lifecycle_root,
)


def config():
    return configuration({
        "repo_root": "/opt/forgeflow", "python": "/opt/forgeflow/.venv/bin/python",
        "sandbox_root": "/var/lib/forgeflow/sandbox", "case_root": "/var/lib/forgeflow/api/cases",
        "data_root": "/var/lib/forgeflow/api/runs", "socket": "/run/forgeflow/executor.sock",
        "container": "forgeflow-odoo", "scenario": "A", "odoo_port": 18069, "api_port": 8000,
    })


@pytest.mark.parametrize("model", ["deepseek-flash", "deepseek-v4-pro"])
def test_known_deepseek_models_share_reasoning_default_in_scoped_patch(tmp_path, model):
    from forgeflow.api.dsh_permissions import scoped_patch

    patch = vps_runtime.dsh_patch(config(), model, "https://api.deepseek.com/v1",
                                 provider="deepseek", api_key_env="DEEPSEEK_API_KEY")
    base = tmp_path / "base.json"
    base.write_text(json.dumps(patch))
    evidence = tmp_path / "owner-scope.json"
    evidence.write_text("{}")
    # SDK and CLI both use scoped_patch. Scope isolation must preserve the
    # provider default while keeping the configured model and output cap.
    for rows in (patch, json.loads(scoped_patch(base, evidence).read_text())):
        profile = next(row for row in rows if row.get("id") == "llm-pi-ai")["config"]["providers"]["deepseek"]
        assert profile["reasoning"] == "max"
        assert profile["models"] == [{"id": model, "input": ["text", "image"], "maxTokens": 8192}]


@pytest.mark.parametrize(("provider", "model"), [
    ("deepseek", "custom-model"),
    ("deepseek", "deepseek-flash-custom"),
    ("nebius", "deepseek-flash"),
    ("nebius", "nvidia/Nemotron-3.5-Super-VL"),
    ("custom", "deepseek-v4-pro"),
])
def test_other_routes_do_not_acquire_unverified_reasoning_capabilities(provider, model):
    patch = vps_runtime.dsh_patch(config(), model, "https://example.invalid/v1", provider=provider)
    profile = next(row for row in patch if row.get("id") == "llm-pi-ai")["config"]["providers"][provider]
    assert "reasoning" not in profile
    assert "reasoningEfforts" not in profile["models"][0]
    assert profile["models"] == [{"id": model, "input": ["text", "image"], "maxTokens": 8192}]


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


def test_api_process_receives_only_the_dedicated_dsh_supplementary_group(tmp_path, monkeypatch):
    from types import SimpleNamespace

    settings = config()
    socket = tmp_path / "executor.sock"
    socket.touch()
    settings["socket"] = str(socket)
    monkeypatch.setattr(vps_runtime.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(vps_runtime.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(vps_runtime.threading, "Event", lambda: SimpleNamespace(
        is_set=lambda: False, wait=lambda _timeout: True, set=lambda: None))
    monkeypatch.setattr(vps_runtime.subprocess, "run", lambda *_args, **_kwargs: SimpleNamespace(returncode=0))
    monkeypatch.setattr(vps_runtime, "container_boot", lambda _name: ("container", "boot", 0))
    monkeypatch.setattr(vps_runtime, "prepare_layout", lambda _config: (
        {"uid": 11001, "gid": 11001, "dsh_gid": 11002}, {"PATH": "/usr/bin"}))
    actual_lstat = Path.lstat
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(
        st_mode=0o140660, st_uid=0, st_gid=11001) if path == socket else actual_lstat(path))
    launches = []

    def launch(command, **options):
        launches.append((command, options))
        return SimpleNamespace(poll=lambda: None)

    monkeypatch.setattr(vps_runtime.subprocess, "Popen", launch)
    monkeypatch.setattr(vps_runtime, "stop_child", lambda *_args: None)
    assert vps_runtime._run_locked(settings, tmp_path / "config.json") == 0
    assert len(launches) == 2
    options = launches[1][1]
    assert options["user"] == options["group"] == 11001
    assert options["extra_groups"] == [11002]
    assert options["umask"] == 0o077


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


def test_api_environment_keeps_the_independent_assistant_binding():
    env = api_environment(config(), {
        "FORGEFLOW_MODEL_NAME": "deepseek-flash",
        "FORGEFLOW_MODEL_BASE_URL": "https://api.deepseek.com/v1",
        "FORGEFLOW_MODEL_API_KEY": "deepseek-key",
        "FORGEFLOW_ASSISTANT_PROFILE": "deepseek_flash",
        "DEEPSEEK_API_KEY": "deepseek-key",
    })
    assert env["FORGEFLOW_ASSISTANT_PROFILE"] == "deepseek_flash"
    assert env["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert env["FORGEFLOW_DSH_MODEL"] == "deepseek-flash"
    assert "FORGEFLOW_DSH_REASONING_EFFORT" not in env
    assert env["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert env["NEBIUS_API_KEY"] == "deepseek-key"
    assert assistant_binding(env) == {
        "profile": "deepseek_flash",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "deepseek-key",
    }


def test_assistant_binding_falls_back_to_the_platform_model():
    env = api_environment(config(), {
        "FORGEFLOW_MODEL_NAME": "deepseek-flash",
        "FORGEFLOW_MODEL_BASE_URL": "https://api.deepseek.com/v1",
        "FORGEFLOW_MODEL_API_KEY": "deepseek-key",
    })
    assert assistant_binding(env) == {
        "profile": "deepseek_flash",
        "provider": "deepseek",
        "model": "deepseek-flash",
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "api_key": "deepseek-key",
    }


@pytest.mark.parametrize("effort", [None, "", "  ", "off", " high ", "max", "unknown-level"])
def test_api_environment_only_forwards_explicit_reasoning_choices(effort):
    source = {"FORGEFLOW_ASSISTANT_PROFILE": "nemotron"}
    if effort is not None:
        source["FORGEFLOW_DSH_REASONING_EFFORT"] = effort
    env = api_environment(config(), source)
    if effort and effort.strip():
        assert env["FORGEFLOW_DSH_REASONING_EFFORT"] == effort.strip()
    else:
        assert "FORGEFLOW_DSH_REASONING_EFFORT" not in env


def _patch_for(key_env: str, tmp_path):
    provider = "deepseek" if key_env == "DEEPSEEK_API_KEY" else "nebius"
    path = tmp_path / "assistant.patch.json"
    path.write_text(json.dumps([
        {"id": "agent-default-model", "config": {"provider": provider, "model": "test-model"}},
        {"id": "llm-pi-ai", "config": {"providers": {provider: {
            "apiKeyEnv": key_env, "api": "openai-completions",
            "baseURL": "https://example.test/v1", "models": [{"id": "test-model"}]}}}},
    ]))
    return path


def test_launcher_passes_only_the_selected_provider_key(tmp_path):
    evidence = tmp_path / "scope.json"
    evidence.write_text("{}")
    deepseek = _patch_for("DEEPSEEK_API_KEY", tmp_path)
    environment = assistant_launch_environment(
        {"home": str(tmp_path)}, evidence, deepseek,
        source={"DEEPSEEK_API_KEY": "deepseek-key", "NEBIUS_API_KEY": "nebius-key"})
    assert environment["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert "NEBIUS_API_KEY" not in environment
    assert assistant_key_env(deepseek) == "DEEPSEEK_API_KEY"


def test_launcher_accepts_a_deepseek_only_host_and_rejects_a_missing_selected_key(tmp_path):
    evidence = tmp_path / "scope.json"
    evidence.write_text("{}")
    deepseek = _patch_for("DEEPSEEK_API_KEY", tmp_path)
    environment = assistant_launch_environment(
        {"home": str(tmp_path)}, evidence, deepseek,
        source={"DEEPSEEK_API_KEY": "deepseek-key"})
    assert environment["DEEPSEEK_API_KEY"] == "deepseek-key"
    with pytest.raises(ValueError, match="assistant_model_key_required"):
        assistant_launch_environment(
            {"home": str(tmp_path)}, evidence, deepseek,
            source={"NEBIUS_API_KEY": "nebius-key"})


def test_launcher_uses_installed_native_modules_without_caller_cache_override(tmp_path):
    patch = _patch_for("DEEPSEEK_API_KEY", tmp_path)
    environment = assistant_launch_environment(
        {"home": str(tmp_path)}, tmp_path / "scope.json", patch,
        source={"DEEPSEEK_API_KEY": "test-key", "NARB_DISABLE_NATIVE_CACHE": "0",
                "NARB_NATIVE_CACHE_DIR": "/untrusted"})
    assert environment["NARB_DISABLE_NATIVE_CACHE"] == "1"
    assert "NARB_NATIVE_CACHE_DIR" not in environment


def test_vps_launch_files_preserve_the_selected_provider_keys():
    root = Path(__file__).resolve().parents[1]
    assert "DEEPSEEK_API_KEY" in (root / "deploy/vps/forgeflow-dsh-node").read_text()
    assert "DEEPSEEK_API_KEY" in (root / "deploy/vps/forgeflow-dsh.sudoers").read_text()
    source = (root / "scripts/vps_reviewers.py").read_text()
    assert "DEEPSEEK_API_KEY" in source
    assert "FORGEFLOW_ASSISTANT_PROFILE" in source


def test_launcher_prefers_the_trusted_config_key_env(tmp_path):
    evidence = tmp_path / "scope.json"
    evidence.write_text("{}")
    patch = tmp_path / "assistant.patch.json"
    patch.write_text("[]")
    environment = assistant_launch_environment(
        {"home": str(tmp_path), "api_key_env": "DEEPSEEK_API_KEY"},
        evidence, patch,
        source={"DEEPSEEK_API_KEY": "deepseek-key", "NEBIUS_API_KEY": "nebius-key"})
    assert environment["DEEPSEEK_API_KEY"] == "deepseek-key"
    assert "NEBIUS_API_KEY" not in environment


def test_api_environment_can_match_local_candidate_flags():
    env = api_environment(config(), {
        "FORGEFLOW_LOCAL_CANDIDATES": "1",
        "FORGEFLOW_LOCAL_REVISIONS": "1",
        "FORGEFLOW_LOCAL_CANDIDATE_PROFILE": "quantity-cost-capacity-v2",
    })
    assert env["FORGEFLOW_LOCAL_CANDIDATES"] == "1"
    assert env["FORGEFLOW_LOCAL_REVISIONS"] == "1"
    assert env["FORGEFLOW_LOCAL_CANDIDATE_PROFILE"] == "quantity-cost-capacity-v2"
    default = api_environment(config(), {})
    assert default["FORGEFLOW_LOCAL_CANDIDATES"] == "0"
    assert "FORGEFLOW_LOCAL_CANDIDATE_PROFILE" not in default


def test_blank_candidate_profile_is_normalized_for_generator_and_consumer(monkeypatch):
    from forgeflow.product.local import CANDIDATE_PROFILE_ENV
    monkeypatch.delenv(CANDIDATE_PROFILE_ENV, raising=False)
    assert candidate_profile_setting() is None
    monkeypatch.setenv(CANDIDATE_PROFILE_ENV, "   ")
    assert candidate_profile_setting() is None
    env = api_environment(config(), {})
    assert CANDIDATE_PROFILE_ENV not in env
    monkeypatch.setenv(CANDIDATE_PROFILE_ENV, "not-a-real-profile")
    with pytest.raises(ValueError):
        candidate_profile_setting()

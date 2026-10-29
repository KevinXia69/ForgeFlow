from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import shlex
import shutil
import subprocess
import sys
import tarfile
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

pytest.importorskip("harbor")

from forgeflow.agents.pi_nebius import (
    AGENT_USER,
    DEFAULT_MODEL,
    MODEL_CONTEXT_WINDOW,
    MODEL_DATA_PACKAGE_INTEGRITY,
    NATIVE_FEEDBACK_EXTENSION,
    NATIVE_PLANNER_EXTENSION,
    NATIVE_RECOVERY_EXTENSION,
    OUTPUT_POLICY_EXTENSION,
    OUTPUT_POLICY_SOURCE,
    PI_COMMIT,
    PI_VERSION,
    TOOL_AUDIT_EXTENSION,
    PiNebius,
    _archive_pi_source,
    _build_log_export_command,
    _validate_model_data,
    _validate_pi_source,
    build_models_config,
    build_pi_command,
)
from forgeflow.agents.pi_source_patch import PROVIDER_SOURCE, patch_provider_source
from forgeflow.config import NebiusConfig, resolve_execution_guidance


def test_config_is_secret_free_and_uses_pinned_pi_environment_syntax(tmp_path):
    config = NebiusConfig(tmp_path, api_key="private-nebius-secret", model=DEFAULT_MODEL)
    result = build_models_config(config, DEFAULT_MODEL)
    provider = result["providers"]["nebius"]
    assert provider["apiKey"] == "$NEBIUS_API_KEY"
    assert provider["api"] == "openai-completions"
    assert provider["models"][0]["id"] == DEFAULT_MODEL
    assert "maxTokens" not in provider["models"][0]
    assert provider["compat"]["maxTokensField"] == "max_tokens"
    assert "private-nebius-secret" not in json.dumps(result)


def test_output_budget_experiment_changes_only_max_tokens(tmp_path):
    config = NebiusConfig(tmp_path, api_key="private-nebius-secret", model=DEFAULT_MODEL)
    baseline = build_models_config(config, DEFAULT_MODEL)
    experiment = build_models_config(config, DEFAULT_MODEL, max_output_tokens=32768)
    assert experiment["providers"]["nebius"]["models"][0]["maxTokens"] == 32768
    del experiment["providers"]["nebius"]["models"][0]["maxTokens"]
    assert experiment == baseline


@pytest.mark.parametrize("output_budget", [None, 32768])
def test_thinking_off_changes_only_reviewed_sampling_parameter(tmp_path, output_budget):
    config = NebiusConfig(tmp_path, model=DEFAULT_MODEL)
    baseline = build_models_config(config, DEFAULT_MODEL, max_output_tokens=output_budget)
    explicit_default = build_models_config(
        config, DEFAULT_MODEL, max_output_tokens=output_budget, thinking_mode="provider-default",
    )
    assert explicit_default == baseline
    model = baseline["providers"]["nebius"]["models"][0]
    assert model["samplingParams"] == {"temperature": 1.0, "top_p": 0.95}
    assert model["reasoning"] is True
    experiment = build_models_config(
        config, DEFAULT_MODEL, max_output_tokens=output_budget, thinking_mode="off",
    )
    sampling = experiment["providers"]["nebius"]["models"][0]["samplingParams"]
    assert sampling.pop("chat_template_kwargs") == {"enable_thinking": False}
    assert experiment == baseline


@pytest.mark.parametrize("mode", [None, True, 0, {}, "high", "OFF"])
def test_invalid_thinking_mode_fails_before_agent_or_model_configuration(tmp_path, mode):
    with pytest.raises(ValueError, match="thinking_mode"):
        build_models_config(NebiusConfig(tmp_path), DEFAULT_MODEL, thinking_mode=mode)
    with pytest.raises(ValueError, match="thinking_mode"):
        PiNebius(logs_dir=tmp_path / "logs", thinking_mode=mode)
    assert not (tmp_path / "logs").exists()


def test_reviewed_mode_does_not_allow_arbitrary_thinking_kwargs(tmp_path):
    with pytest.raises(ValueError, match="Arbitrary thinking arguments"):
        PiNebius(logs_dir=tmp_path / "logs", thinking_mode="off", thinking="high")


def test_provider_default_uses_only_explicit_extension_and_explicit_limit_bypasses_it():
    default = shlex.split(build_pi_command("task", DEFAULT_MODEL))
    assert "--no-extensions" in default
    assert default[default.index("-e") + 1] == OUTPUT_POLICY_EXTENSION
    explicit = shlex.split(build_pi_command(
        "task", DEFAULT_MODEL, provider_default_output=False,
    ))
    assert "--no-extensions" in explicit
    assert [explicit[i + 1] for i, item in enumerate(explicit) if item == "-e"] == [
        TOOL_AUDIT_EXTENSION,
    ]


def test_provider_default_extension_removes_only_limits_and_audits_no_content(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed to validate the Pi request extension.")
    audit = tmp_path / "policy.jsonl"
    script = """
import { pathToFileURL } from 'node:url';
const { default: extension } = await import(pathToFileURL(process.argv[1]));
extension({});
const handler = globalThis.__forgeflowProviderDefaultOutput;
const original = {
  model: 'fixture', max_tokens: 123, max_completion_tokens: 456,
  messages: [{role:'user', content:'private-fixture-message'}],
  tools: [{name:'fixture'}], temperature:1, top_p:0.95,
};
if (handler(original, {provider:'other'}) !== original)
  throw Error('unrelated provider changed');
const result = handler(original, {provider:'nebius'});
if (Object.hasOwn(result,'max_tokens') || Object.hasOwn(result,'max_completion_tokens'))
  throw Error('output limit remained');
if (original.max_tokens !== 123 || original.max_completion_tokens !== 456)
  throw Error('input was mutated');
const {max_tokens, max_completion_tokens, ...rest} = original;
if (JSON.stringify(result) !== JSON.stringify(rest)) throw Error('unrelated field changed');
"""
    subprocess.run(
        [node, "--input-type=module", "-e", script, str(OUTPUT_POLICY_SOURCE)],
        env={**os.environ, "FORGEFLOW_OUTPUT_POLICY_AUDIT_PATH": str(audit)},
        check=True, capture_output=True, text=True,
    )
    record = json.loads(audit.read_text())
    assert record == {
        "request_index": 1, "policy": "provider_default",
        "policy_version": "all-requests-v2",
        "max_tokens_present": False, "max_completion_tokens_present": False,
    }
    assert "private-fixture-message" not in audit.read_text()


@pytest.mark.parametrize(
    ("profile", "expected_extension"),
    [
        ("cp-sat-native-v1", NATIVE_PLANNER_EXTENSION),
        ("cp-sat-native-v2", NATIVE_FEEDBACK_EXTENSION),
        ("cp-sat-recovery-v1", NATIVE_RECOVERY_EXTENSION),
    ],
)
def test_native_planner_is_explicitly_loaded_with_default_policy_and_both_are_independent(
    profile, expected_extension,
):
    default = shlex.split(build_pi_command(
        "task", DEFAULT_MODEL, planner_profile=profile,
    ))
    extensions = [default[i + 1] for i, value in enumerate(default) if value == "-e"]
    assert extensions == [OUTPUT_POLICY_EXTENSION, TOOL_AUDIT_EXTENSION, expected_extension]
    tool_name = "prepare_recovery" if profile == "cp-sat-recovery-v1" else "plan_recovery"
    assert tool_name in default[default.index("--append-system-prompt") + 1]
    assert default.index("--append-system-prompt") < default.index("--")
    explicit = shlex.split(build_pi_command(
        "task", DEFAULT_MODEL, planner_profile=profile,
        provider_default_output=False,
    ))
    assert [explicit[i + 1] for i, value in enumerate(explicit) if value == "-e"] == [
        TOOL_AUDIT_EXTENSION, expected_extension,
    ]


def test_provider_patch_refuses_unreviewed_source():
    with pytest.raises(RuntimeError, match="reviewed patch target"):
        patch_provider_source(b"unknown provider")


def test_provider_patch_is_only_in_build_archive_and_after_existing_payload_hook(tmp_path):
    root = OUTPUT_POLICY_SOURCE.parents[3]
    pinned_path = root / "vendor/pi-mono" / PROVIDER_SOURCE
    original = pinned_path.read_bytes()
    # Worktrees may share the pinned checkout through a directory symlink.
    # Archive a regular test source tree; keep the production symlink rejection.
    source = tmp_path / "pi-source"
    path = source / PROVIDER_SOURCE
    path.parent.mkdir(parents=True)
    path.write_bytes(original)
    modified, record = patch_provider_source(original)
    assert modified.index(b"const forgeflowPayloadPolicy") > modified.index(
        b"const nextParams = await options?.onPayload"
    )
    assert modified.index(b"const forgeflowPayloadPolicy") < modified.index(
        b"client.chat.completions.create(params"
    )
    archive_path = tmp_path / "patched-pi.tar.gz"
    archived_record = _archive_pi_source(
        source, archive_path, provider_payload_patch=True,
    )
    with tarfile.open(archive_path) as archive:
        assert archive.getnames().count("pi/" + PROVIDER_SOURCE) == 1
        assert archive.extractfile("pi/" + PROVIDER_SOURCE).read() == modified
    assert archived_record == record
    assert hashlib.sha256(modified).hexdigest() == record["execution_sha256"]
    assert path.read_bytes() == original
    assert pinned_path.read_bytes() == original


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "32768", MODEL_CONTEXT_WINDOW])
def test_output_budget_rejected_before_model_config_or_agent_creation(tmp_path, value):
    config = NebiusConfig(tmp_path, api_key="private-nebius-secret", model=DEFAULT_MODEL)
    with pytest.raises(ValueError, match="max_output_tokens"):
        build_models_config(config, DEFAULT_MODEL, max_output_tokens=value)
    with pytest.raises(ValueError, match="max_output_tokens"):
        PiNebius(logs_dir=tmp_path / "logs", max_output_tokens=value)


def test_log_pipeline_preserves_failure_and_quotes_task_text(tmp_path):
    script = tmp_path / "fake-pi.sh"
    script.write_text("#!/bin/bash\nprintf '%s\\n' \"$@\"\nexit 17\n", encoding="utf-8")
    script.chmod(0o700)
    unexpected = tmp_path / "must-not-exist"
    instruction = f"Text ' quoted; $(touch {unexpected}) `touch {unexpected}`"
    log = tmp_path / "agent output.jsonl"
    command = build_pi_command(
        instruction, DEFAULT_MODEL, executable=(str(script),), output_file=str(log)
    )
    result = subprocess.run(["bash", "-c", command], capture_output=True, text=True)
    assert result.returncode == 17
    assert instruction in log.read_text(encoding="utf-8")
    assert not unexpected.exists()
    assert "--max-turns" not in command


@pytest.mark.parametrize("profile", ["odoo-read-v1", "odoo-schedule-v1"])
def test_guidance_appends_before_task_without_replacing_or_reinterpreting_it(tmp_path, profile):
    script = tmp_path / "capture-arguments.py"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    unexpected = tmp_path / "must-not-exist"
    instruction = f"Original task; $(touch '{unexpected}') and 'quoted' content."
    log = tmp_path / "arguments.json"
    command = build_pi_command(
        instruction, DEFAULT_MODEL, executable=(sys.executable, str(script)),
        output_file=str(log), execution_guidance=profile,
    )
    subprocess.run(["bash", "-c", command], check=True, capture_output=True)
    arguments = json.loads(log.read_text())
    append_index = arguments.index("--append-system-prompt")
    assert arguments[append_index + 1] == resolve_execution_guidance(profile)["text"]
    assert append_index < arguments.index("--")
    assert arguments[-2:] == ["--", instruction]
    assert "--system-prompt" not in arguments
    assert "--no-context-files" in arguments
    assert not unexpected.exists()
    default_command = build_pi_command(instruction, DEFAULT_MODEL)
    assert "--append-system-prompt" not in default_command
    assert default_command == build_pi_command(instruction, DEFAULT_MODEL, execution_guidance="none")


def test_guidance_bundle_has_reviewed_exact_content_hash_and_rejects_unknown_profiles():
    guidance = resolve_execution_guidance("odoo-read-v1")
    assert guidance["version"] == 1
    assert len(guidance["text"].splitlines()) == 5
    assert guidance["sha256"] == "0a2ed0df4380c5f2338a5e06753cd5bae9e5d8f71b980ca84a57367cedd6f7e3"
    assert hashlib.sha256(guidance["text"].encode()).hexdigest() == guidance["sha256"]
    assert guidance["instruction_modified"] is False
    assert resolve_execution_guidance()["text"] == ""
    with pytest.raises(ValueError, match="Unknown execution guidance"):
        build_pi_command("Task", DEFAULT_MODEL, execution_guidance="arbitrary-file.txt")


def test_privileged_trace_export_skips_symlinks_and_unknown_files(tmp_path):
    source = tmp_path / "private logs"
    destination = tmp_path / "harbor logs"
    source.mkdir()
    destination.mkdir()
    (source / "isolation.json").write_text('{"passed": true}', encoding="utf-8")
    secret = tmp_path / "private-file"
    secret.write_text("must not be exported", encoding="utf-8")
    (source / "pi.txt").symlink_to(secret)
    (source / "unrequested.txt").write_text("not a trace", encoding="utf-8")
    command = _build_log_export_command(str(source), str(destination))
    subprocess.run(["bash", "-c", command], check=True)
    assert (destination / "isolation.json").read_text() == '{"passed": true}'
    assert sorted(path.name for path in destination.iterdir()) == ["isolation.json"]
    assert secret.read_text() == "must not be exported"


def _recovery_export_fixture(tmp_path):
    source = tmp_path / "private logs"
    destination = tmp_path / "harbor logs"
    plan = source / "recovery" / ("a" * 64)
    plan.mkdir(parents=True)
    destination.mkdir()
    for name in ("request", "snapshot", "blueprint", "plan", "change-set", "baseline",
                 "binding", "manifest"):
        (plan / (name + ".json")).write_text(json.dumps({"artifact": name}) + "\n")
    return source, destination, plan


def _run_recovery_export(source, destination):
    completed = subprocess.run(
        ["bash", "-c", _build_log_export_command(str(source), str(destination))],
        env={"PATH": os.environ["PATH"], "PYTHONDONTWRITEBYTECODE": "1"},
        check=True, capture_output=True, text=True, timeout=30,
    )
    assert completed.stdout == "" and completed.stderr == ""
    return json.loads((destination / "recovery-export.json").read_text())


def test_recovery_export_preserves_whitelisted_originals_journal_bindings_and_hashes(tmp_path):
    source, destination, plan = _recovery_export_fixture(tmp_path)
    (source / "recovery" / "active-plan.json").write_text(json.dumps({"plan_id": plan.name}))
    (source / "recovery" / ("failure-" + "b" * 32 + ".json")).write_text('{"phase":"apply"}')
    for name in ("prepared.json", "success.json", "journal.jsonl", "readback-" + "c" * 32 + ".json"):
        (plan / name).write_text('{"synthetic":true}\n')
    binding = plan / "bindings" / ("d" * 64) / ("e" * 64 + ".json")
    binding.parent.mkdir(parents=True)
    binding.write_text('{"record":{"id":1}}\n')
    report = _run_recovery_export(source, destination)
    assert report["complete"] is True and report["skipped_count"] == 0
    expected = {path.relative_to(source / "recovery").as_posix(): path.read_bytes()
                for path in (source / "recovery").rglob("*") if path.is_file()}
    actual = {path.relative_to(destination / "recovery").as_posix(): path.read_bytes()
              for path in (destination / "recovery").rglob("*") if path.is_file()}
    assert actual == expected
    assert {item["path"]: item["sha256"] for item in report["copied_files"]} == {
        name: hashlib.sha256(value).hexdigest() for name, value in expected.items()
    }
    assert report["total_bytes"] == sum(map(len, expected.values()))


def test_recovery_export_rejects_symlinks_unknown_paths_and_nonregular_artifacts(tmp_path):
    source, destination, plan = _recovery_export_fixture(tmp_path)
    secret = tmp_path / "private-file"
    secret.write_text("fixture-secret-not-an-artifact")
    (plan / "baseline.json").unlink()
    (plan / "baseline.json").symlink_to(secret)
    (plan / "prepared.json").mkdir()
    os.mkfifo(plan / "success.json")
    (plan / "api_key").write_text("fixture-secret-not-an-artifact")
    arbitrary = plan / "arbitrary-directory"
    arbitrary.mkdir()
    (arbitrary / "journal.jsonl").write_text("fixture-secret-not-an-artifact")
    (source / "recovery" / ("b" * 64)).symlink_to(plan, target_is_directory=True)
    (source / "recovery" / ("failure-" + "f" * 32 + ".json")).symlink_to(secret)
    binding_base = plan / "bindings" / ("c" * 64)
    binding_base.mkdir(parents=True)
    (binding_base / ("d" * 64 + ".json")).symlink_to(secret)
    (binding_base / "unknown.json").write_text("fixture-secret-not-an-artifact")
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False and report["skipped_count"] >= 6
    assert report["ignored_entries"] >= 3
    exported = [path for path in destination.rglob("*") if path.is_file()]
    assert all("fixture-secret" not in path.read_text() for path in exported)
    assert not (destination / "recovery" / plan.name / "arbitrary-directory").exists()
    assert secret.read_text() == "fixture-secret-not-an-artifact"


def test_recovery_export_rejects_symlinked_recovery_and_binding_roots(tmp_path):
    source, destination, plan = _recovery_export_fixture(tmp_path)
    private = tmp_path / "private"
    private.mkdir()
    (private / ("b" * 64 + ".json")).write_text("fixture-secret")
    (plan / "bindings").symlink_to(private, target_is_directory=True)
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False
    assert any(item["reason"] == "not_regular_directory" for item in report["skipped"])
    assert not (destination / "recovery" / plan.name / "bindings").exists()
    shutil.rmtree(destination)
    destination.mkdir()
    shutil.rmtree(source / "recovery")
    (source / "recovery").symlink_to(private, target_is_directory=True)
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False and report["copied_files"] == []


def test_recovery_export_size_and_missing_artifacts_are_explicit(tmp_path):
    source, destination, plan = _recovery_export_fixture(tmp_path)
    (plan / "binding.json").unlink()
    with (plan / "snapshot.json").open("wb") as handle:
        handle.truncate(16 * 1024 * 1024 + 1)
    binding = plan / "bindings" / ("d" * 64) / ("e" * 64 + ".json")
    binding.parent.mkdir(parents=True)
    with binding.open("wb") as handle:
        handle.truncate(64 * 1024 + 1)
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False
    reasons = {item["path"]: item["reason"] for item in report["skipped"]}
    assert reasons[plan.name + "/binding.json"] == "required_artifact_missing"
    assert reasons[plan.name + "/snapshot.json"] == "file_size_limit"
    assert reasons[binding.relative_to(source / "recovery").as_posix()] == "file_size_limit"
    assert not (destination / "recovery" / plan.name / "snapshot.json").exists()


def test_recovery_export_count_limits_mark_incomplete_without_copying_unknown_names(tmp_path):
    source, destination, plan = _recovery_export_fixture(tmp_path)
    for number in range(65):
        (plan / ("readback-" + f"{number:032x}" + ".json")).write_text("{}")
    for number in range(129):
        (source / "recovery" / ("failure-" + f"{number:032x}" + ".json")).write_text("{}")
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False
    assert {item["reason"] for item in report["skipped"]} == {
        "readback_count_limit", "failure_count_limit",
    }
    assert len(list((destination / "recovery" / plan.name).glob("readback-*.json"))) == 64
    assert len(list((destination / "recovery").glob("failure-*.json"))) == 128


def test_recovery_export_total_size_cap_is_reported(tmp_path):
    source, destination, plan = _recovery_export_fixture(tmp_path)
    for number in range(9):
        with (plan / ("readback-" + f"{number:032x}" + ".json")).open("wb") as handle:
            handle.truncate(16 * 1024 * 1024)
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False
    assert report["total_bytes"] <= 128 * 1024 * 1024
    assert any(item["reason"] == "total_size_limit" for item in report["skipped"])


def test_recovery_export_never_follows_destination_symlink(tmp_path):
    source, destination, _ = _recovery_export_fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (destination / "recovery").symlink_to(outside, target_is_directory=True)
    report = _run_recovery_export(source, destination)
    assert report["complete"] is False and report["copied_files"] == []
    assert list(outside.iterdir()) == []


def test_pinned_snapshot_validation_rejects_changed_version(tmp_path):
    source = tmp_path / "pi-mono"
    package = source / "packages/coding-agent/package.json"
    package.parent.mkdir(parents=True)
    package.write_text(json.dumps({"version": PI_VERSION}), encoding="utf-8")
    (source / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "pi-mono.source.json").write_text(
        json.dumps({"commit": PI_COMMIT}), encoding="utf-8"
    )
    _validate_pi_source(source)
    package.write_text(json.dumps({"version": "future"}), encoding="utf-8")
    with pytest.raises(RuntimeError, match="bound to Pi"):
        _validate_pi_source(source)


def test_source_archive_excludes_git_dependencies_and_prior_builds(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "LICENSE").write_text("license", encoding="utf-8")
    for name in (".git", "node_modules", "dist", ".pi"):
        (source / name).mkdir()
        (source / name / "excluded").write_text("private", encoding="utf-8")
    target = tmp_path / "source.tar.gz"
    _archive_pi_source(source, target)
    with tarfile.open(target) as archive:
        assert archive.getnames() == ["pi", "pi/LICENSE"]


def test_model_data_detects_tampering_before_upload(tmp_path):
    (tmp_path / "data").mkdir()
    content = b'{"models": []}'
    data_file = tmp_path / "data/provider.json"
    data_file.write_bytes(content)
    (tmp_path / "provenance.json").write_text(
        json.dumps(
            {
                "integrity": MODEL_DATA_PACKAGE_INTEGRITY,
                "pi_source_commit": PI_COMMIT,
                "files_sha256": {"provider.json": hashlib.sha256(content).hexdigest()},
            }
        ),
        encoding="utf-8",
    )
    assert _validate_model_data(tmp_path) == tmp_path / "data"
    data_file.write_text("modified", encoding="utf-8")
    with pytest.raises(RuntimeError, match="content has changed"):
        _validate_model_data(tmp_path)


def test_package_update_failure_stops_installation_before_permissions_change(tmp_path, monkeypatch):
    monkeypatch.setattr("forgeflow.agents.pi_nebius._validate_pi_source", lambda source: None)
    monkeypatch.setattr("forgeflow.agents.pi_nebius._validate_model_data", lambda data: data)
    agent = PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path)
    agent.exec_as_root = AsyncMock(side_effect=RuntimeError("Capture command"))
    with pytest.raises(RuntimeError, match="Capture command"):
        asyncio.run(agent.install(SimpleNamespace()))
    command = agent.exec_as_root.call_args_list[0].kwargs["command"]
    reached_permissions = tmp_path / "permissions-changed"
    stubs = (
        "command() { if [[ \"$1\" == -v && \"$2\" == curl ]]; then return 1; "
        "else builtin command \"$@\"; fi; }; "
        "apt-get() { return 41; }; "
        f"id() {{ touch {shlex.quote(str(reached_permissions))}; }}; "
        "install() { :; }; chown() { :; }; chmod() { :; }; useradd() { :; }; "
    )
    result = subprocess.run(["bash", "-c", stubs + command], capture_output=True, text=True)
    assert result.returncode == 41
    assert not reached_permissions.exists()


@pytest.mark.parametrize(
    ("max_output_tokens", "execution_guidance", "thinking_mode"),
    [
        (16384, "none", "provider-default"), (32768, "none", "provider-default"),
        (16384, "odoo-read-v1", "provider-default"), (None, "none", "off"),
        (32768, "none", "off"), (None, "odoo-schedule-v1", "off"),
    ],
)
def test_run_passes_only_nebius_key_via_env_with_private_config(
    tmp_path, monkeypatch, max_output_tokens, execution_guidance, thinking_mode,
):
    config = NebiusConfig(tmp_path, api_key="private-nebius-secret", model=DEFAULT_MODEL)
    monkeypatch.setattr("forgeflow.agents.pi_nebius.load_config", lambda root: config)
    agent = PiNebius(
        logs_dir=tmp_path / "logs", workspace_root=tmp_path,
        max_output_tokens=max_output_tokens, execution_guidance=execution_guidance,
        thinking_mode=thinking_mode,
    )
    states = []

    class Environment:
        default_user = None

        @contextlib.contextmanager
        def with_default_user(self, user):
            previous = self.default_user
            self.default_user = user
            states.append(user)
            try:
                yield
            finally:
                self.default_user = previous

    environment = Environment()
    agent._upload_config_text = AsyncMock()
    agent._configure_budget = AsyncMock()
    agent.exec_as_agent = AsyncMock()
    agent.exec_as_root = AsyncMock()
    asyncio.run(agent.run("Inspect current orders.", environment, SimpleNamespace()))
    assert states == [AGENT_USER, AGENT_USER]
    assert environment.default_user is None
    upload = agent._upload_config_text.call_args.kwargs
    assert "private-nebius-secret" not in upload["content"]
    uploaded_model = json.loads(upload["content"])["providers"]["nebius"]["models"][0]
    assert uploaded_model.get("maxTokens") == max_output_tokens
    assert ("maxTokens" in uploaded_model) == (max_output_tokens is not None)
    expected_sampling = {"temperature": 1.0, "top_p": 0.95}
    if thinking_mode == "off":
        expected_sampling["chat_template_kwargs"] = {"enable_thinking": False}
    assert uploaded_model["samplingParams"] == expected_sampling
    assert uploaded_model["reasoning"] is True
    calls = agent.exec_as_agent.call_args_list
    assert len(calls) == 2
    run = calls[-1].kwargs
    assert run["env"]["NEBIUS_API_KEY"] == "private-nebius-secret"
    assert "OPENAI_API_KEY" not in run["env"]
    assert "private-nebius-secret" not in run["command"]
    assert "--thinking" not in run["command"]
    assert ("--append-system-prompt" in run["command"]) == (execution_guidance != "none")
    assert run["cwd"] == "/workspace"
    assert all(
        run["env"][name] == ""
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy")
    )
    assert agent.exec_as_root.await_count == 2
    assert "pkill -KILL -u forgeflow" in agent.exec_as_root.call_args.kwargs["command"]


def test_isolation_check_needs_no_credentials_and_restores_environment_user(tmp_path, monkeypatch):
    def no_credentials(root):
        pytest.fail("Filesystem validation must not load model credentials.")

    monkeypatch.setattr("forgeflow.agents.pi_nebius.load_config", no_credentials)
    agent = PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path)

    class Environment:
        default_user = "original-user"

        @contextlib.contextmanager
        def with_default_user(self, user):
            previous = self.default_user
            self.default_user = user
            try:
                yield
            finally:
                self.default_user = previous

    environment = Environment()

    async def validate_as_agent(*args, **kwargs):
        assert environment.default_user == AGENT_USER
        assert kwargs["cwd"] == "/workspace"
        return SimpleNamespace(return_code=0)

    agent.exec_as_agent = AsyncMock(side_effect=validate_as_agent)
    result = asyncio.run(agent.validate_isolation(environment))
    assert result.return_code == 0
    assert environment.default_user == "original-user"


def test_failed_isolation_prevents_config_upload_and_model_execution(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "forgeflow.agents.pi_nebius.load_config",
        lambda root: NebiusConfig(tmp_path, api_key="test-only"),
    )
    agent = PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path)
    agent.validate_isolation = AsyncMock(side_effect=RuntimeError("Unsafe permissions"))
    agent._configure_budget = AsyncMock()
    agent._upload_config_text = AsyncMock()
    agent.exec_as_agent = AsyncMock()
    agent.exec_as_root = AsyncMock()
    with pytest.raises(RuntimeError, match="Unsafe permissions"):
        asyncio.run(agent.run("Task", SimpleNamespace(), SimpleNamespace()))
    agent._upload_config_text.assert_not_called()
    agent.exec_as_agent.assert_not_called()
    assert agent.exec_as_root.await_count == 2


def test_missing_nebius_key_does_not_run_or_upload(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "forgeflow.agents.pi_nebius.load_config",
        lambda root: NebiusConfig(tmp_path),
    )
    agent = PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path)
    agent._upload_config_text = AsyncMock()
    agent.exec_as_agent = AsyncMock()
    with pytest.raises(ValueError, match="NEBIUS_API_KEY"):
        asyncio.run(agent.run("Task", SimpleNamespace(), SimpleNamespace()))
    agent._upload_config_text.assert_not_called()
    agent.exec_as_agent.assert_not_called()


def test_failed_agent_still_cleans_only_its_dedicated_user(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "forgeflow.agents.pi_nebius.load_config",
        lambda root: NebiusConfig(tmp_path, api_key="test-only"),
    )
    agent = PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path)
    environment = SimpleNamespace(
        with_default_user=lambda user: contextlib.nullcontext()
    )
    agent._upload_config_text = AsyncMock()
    agent.exec_as_agent = AsyncMock(side_effect=[None, RuntimeError("Pi failed")])
    agent._configure_budget = AsyncMock()
    agent.exec_as_root = AsyncMock()
    with pytest.raises(RuntimeError, match="Pi failed"):
        asyncio.run(agent.run("Task", environment, SimpleNamespace()))
    command = agent.exec_as_root.call_args.kwargs["command"]
    assert "pkill -KILL -u forgeflow" in command
    assert "odoo" not in command and "postgres" not in command


@pytest.mark.parametrize("kwargs", [{"max_turns": 10}, {"thinking": "high"}])
def test_unsupported_cli_options_fail_instead_of_being_silently_ignored(tmp_path, kwargs):
    with pytest.raises(ValueError):
        PiNebius(logs_dir=tmp_path / "logs", **kwargs)


def test_permission_profile_requires_the_recovery_planner(tmp_path):
    with pytest.raises(ValueError, match="requires cp-sat-recovery-v1"):
        PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path,
                 planner_profile="none", security_profile="permission-split-v1")


def test_permission_profile_run_exports_the_executor_settings(tmp_path, monkeypatch):
    config = NebiusConfig(tmp_path, api_key="private-nebius-secret", model=DEFAULT_MODEL)
    monkeypatch.setattr("forgeflow.agents.pi_nebius.load_config", lambda root: config)
    agent = PiNebius(
        logs_dir=tmp_path / "logs", workspace_root=tmp_path,
        planner_profile="cp-sat-recovery-v1", security_profile="permission-split-v1",
    )
    agent.validate_isolation = AsyncMock()
    agent._configure_budget = AsyncMock()
    agent._upload_config_text = AsyncMock()
    agent.exec_as_agent = AsyncMock()
    agent.exec_as_root = AsyncMock()

    class Environment:
        @contextlib.contextmanager
        def with_default_user(self, user):
            yield

    asyncio.run(agent.run("Inspect current orders.", Environment(), SimpleNamespace()))
    env = agent.exec_as_agent.call_args_list[-1].kwargs["env"]
    assert env["FORGEFLOW_ODOO_KEY_FILE"] == "/etc/odoo/api_key.read"
    assert env["FORGEFLOW_EXECUTOR_SOCKET"] == "/run/forgeflow/executor.sock"
    stop = agent.exec_as_root.call_args_list[1].kwargs["command"]
    assert "executor.ready" in stop
    export = agent.exec_as_root.call_args_list[-1].kwargs["command"]
    assert "/var/lib/forgeflow/recovery" in export


def test_executor_commands_are_valid_bash(tmp_path):
    agent = PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path,
                     planner_profile="cp-sat-recovery-v1",
                     security_profile="permission-split-v1")
    for command in (agent._executor_start_command(), agent._executor_stop_command()):
        checked = subprocess.run(["bash", "-n", "-c", command], capture_output=True, text=True)
        assert checked.returncode == 0, checked.stderr
    start = agent._executor_start_command()
    assert "/run/forgeflow/executor.sock" in start
    assert "/var/lib/forgeflow/recovery" in start
    assert "--agent-user forgeflow" in start
    assert "chmod 0400 /etc/odoo/api_key" in start
    assert "cd /opt/forgeflow/planner" in start
    assert "-m forgeflow.executor.main" in start
    assert "/logs/agent/executor.json" in start

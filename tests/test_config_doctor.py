from __future__ import annotations

import json
import os
import subprocess

import pytest

from forgeflow.config import THINKING_MODES, ConfigError, load_config, validate_thinking_mode
from forgeflow.doctor import run_doctor
from forgeflow.runtime import docker_environment


def test_thinking_modes_are_explicit_and_preserve_provider_default():
    assert THINKING_MODES == ("provider-default", "off")
    assert validate_thinking_mode() == "provider-default"
    assert validate_thinking_mode("off") == "off"


@pytest.mark.parametrize("value", [None, True, 0, {}, [], "ON", "private-invalid-mode"])
def test_unknown_thinking_modes_fail_without_echoing_input(value):
    with pytest.raises(ConfigError, match="Unknown thinking_mode") as error:
        validate_thinking_mode(value)
    assert "private-invalid-mode" not in str(error.value)


def test_explicit_workspace_and_environment_override_without_process_mutation(tmp_path):
    (tmp_path / ".env").write_text(
        "NEBIUS_API_KEY=file-secret\n"
        "NEBIUS_BASE_URL=https://api.tokenfactory.nebius.com/v1\n"
        "NEBIUS_MODEL=file-model\n",
        encoding="utf-8",
    )
    process_before = dict(os.environ)
    config = load_config(
        tmp_path,
        environ={"NEBIUS_API_KEY": "env-secret", "NEBIUS_MODEL": "env-model"},
        require_model=True,
    )
    assert config.api_key == "env-secret"
    assert config.model == "env-model"
    assert dict(os.environ) == process_before
    report = json.dumps(config.public_dict())
    assert "env-secret" not in report
    assert "file-secret" not in report
    assert "env-secret" not in repr(config)
    assert config.public_dict()["key_present"] is True


def test_missing_configuration_allows_offline_work_but_not_model_run(tmp_path):
    config = load_config(tmp_path, environ={"OPENAI_API_KEY": "unrelated-secret"})
    assert config.api_key == ""
    assert config.public_dict()["key_present"] is False
    with pytest.raises(ConfigError, match="NEBIUS_API_KEY.*NEBIUS_MODEL"):
        config.require_model_run()


def test_empty_environment_override_does_not_fall_back_to_file(tmp_path):
    (tmp_path / ".env").write_text(
        "NEBIUS_API_KEY=file-secret\nNEBIUS_MODEL=model\n", encoding="utf-8"
    )
    config = load_config(
        tmp_path, environ={"NEBIUS_API_KEY": "", "OPENAI_API_KEY": "unrelated-secret"}
    )
    assert config.api_key == ""
    with pytest.raises(ConfigError, match="NEBIUS_API_KEY"):
        load_config(tmp_path, environ={"NEBIUS_API_KEY": ""}, require_model=True)


def test_does_not_search_parent_dotenv_or_interpolate_other_provider_key(tmp_path):
    (tmp_path / ".env").write_text(
        "NEBIUS_API_KEY=parent-secret\n", encoding="utf-8"
    )
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert load_config(workspace, environ={}).api_key == ""
    (workspace / ".env").write_text(
        "NEBIUS_API_KEY=${OPENAI_API_KEY}\n", encoding="utf-8"
    )
    with pytest.raises(ConfigError, match="unresolved interpolation") as error:
        load_config(workspace, environ={"OPENAI_API_KEY": "unrelated-secret"})
    assert "unrelated-secret" not in str(error.value)


@pytest.mark.parametrize(
    "url",
    [
        "",
        "http://api.tokenfactory.nebius.com/v1",
        "api.tokenfactory.nebius.com/v1",
        "https:///v1",
        "https://user:secret@api.tokenfactory.nebius.com/v1",
        "https://api.tokenfactory.nebius.com/v1?api_key=secret",
        "https://api.tokenfactory.nebius.com/v1#secret",
        "https://api.tokenfactory.nebius.com:invalid/v1",
        "https://api.tokenfactory.nebius.com:0/v1",
        "https://api.tokenfactory.nebius.com:65536/v1",
        "https://api.tokenfactory.nebius.com/invalid path",
        "https://api.tokenfactory.nebius.com\\secret",
        "https://api.tokenfactory.nebius.com/\x00",
    ],
)
def test_invalid_urls_rejected_without_echoing_values(tmp_path, url):
    with pytest.raises(ConfigError) as error:
        load_config(tmp_path, environ={"NEBIUS_BASE_URL": url})
    assert "secret" not in str(error.value)


def test_doctor_reports_config_presence_without_network_calls(tmp_path, monkeypatch):
    monkeypatch.setattr("forgeflow.doctor.shutil.which", lambda name: None)

    def no_process(*args, **kwargs):
        raise AssertionError("No process should run when Docker is missing")

    monkeypatch.setattr("forgeflow.doctor.subprocess.run", no_process)
    report = run_doctor(
        tmp_path,
        environ={"NEBIUS_API_KEY": "private-secret", "NEBIUS_MODEL": "model"},
    )
    assert report["config"]["key_present"] is True
    assert report["config"]["status"] == "configured"
    assert report["config"]["provider_call_performed"] is False
    assert report["benchmark_run_performed"] is False
    assert report["docker"]["status"] == "missing"
    assert "private-secret" not in json.dumps(report)


def test_doctor_bounds_docker_call_and_redacts_timeout_output(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "forgeflow.doctor.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    calls = []

    def timeout_run(command, **kwargs):
        calls.append((command, kwargs))
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output="private-output", stderr="private-error"
        )

    monkeypatch.setattr("forgeflow.doctor.subprocess.run", timeout_run)
    report = run_doctor(tmp_path, timeout_seconds=0.2, environ={})
    assert report["docker"]["status"] == "timeout"
    assert "private-output" not in json.dumps(report)
    assert "private-error" not in json.dumps(report)
    assert len(calls) == 1
    assert calls[0][0][1] == "info"
    assert calls[0][1]["timeout"] == 0.2


@pytest.mark.parametrize("timeout", [0, -1, 31, float("inf"), float("nan")])
def test_doctor_rejects_unbounded_or_invalid_timeouts(tmp_path, timeout):
    with pytest.raises(ValueError, match="timeout"):
        run_doctor(tmp_path, timeout_seconds=timeout, environ={})


def test_doctor_reports_daemon_failure_without_echoing_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr("forgeflow.doctor.shutil.which", lambda name: "/fake/" + name)
    monkeypatch.setattr(
        "forgeflow.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0], 1, "", "private-docker-endpoint"
        ),
    )
    report = run_doctor(tmp_path, environ={})
    assert report["docker"]["status"] == "unavailable"
    assert report["docker"]["returncode"] == 1
    assert "private-docker-endpoint" not in json.dumps(report)


def test_doctor_reports_selected_docker_facts_only(tmp_path, monkeypatch):
    monkeypatch.setattr("forgeflow.doctor.shutil.which", lambda name: "/fake/" + name)
    monkeypatch.setattr(
        "forgeflow.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0],
            0,
            json.dumps(
                {
                    "server_version": "test-version",
                    "architecture": "aarch64",
                    "operating_system": "Linux",
                    "cpus": 4,
                    "memory_bytes": 8_000_000_000,
                    "unexpected_private_field": "secret",
                }
            ),
            "",
        ),
    )
    report = run_doctor(tmp_path, environ={})
    assert report["docker"]["available"] is True
    assert report["docker"]["cpus"] == 4
    assert "secret" not in json.dumps(report)


def test_doctor_handles_invalid_configuration_without_leaking_url(tmp_path, monkeypatch):
    monkeypatch.setattr("forgeflow.doctor.shutil.which", lambda name: None)
    report = run_doctor(
        tmp_path,
        environ={"NEBIUS_BASE_URL": "https://user:secret@example.com/v1"},
    )
    assert report["config"]["status"] == "invalid"
    assert "secret" not in json.dumps(report)


@pytest.mark.parametrize("output", ["private-invalid-json", "[]", "{}"])
def test_doctor_does_not_claim_availability_on_malformed_output(
    tmp_path, monkeypatch, output
):
    monkeypatch.setattr("forgeflow.doctor.shutil.which", lambda name: "/fake/" + name)
    monkeypatch.setattr(
        "forgeflow.doctor.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, output, ""),
    )
    report = run_doctor(tmp_path, environ={})
    assert report["docker"]["available"] is False
    assert report["docker"]["status"] == "invalid_response"
    assert "private-invalid-json" not in json.dumps(report)


def test_docker_environment_imports_only_local_docker_settings(tmp_path, monkeypatch):
    (tmp_path / ".env").write_text(
        "DOCKER_HOST=unix:///local/forgeflow/docker.sock\n"
        "DOCKER_CONFIG=/local/forgeflow/docker-config\n"
        "DOCKER_CONTEXT=forgeflow\n"
        "NEBIUS_API_KEY=must-not-import-this-secret\n"
        "OPENAI_API_KEY=must-not-import-this-either\n"
        "UNRELATED=file-value\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DOCKER_HOST", "unix:///existing/context/docker.sock")
    process_before = dict(os.environ)
    environment = docker_environment(tmp_path, environ={"PATH": "/test/bin"})
    assert environment == {
        "PATH": "/test/bin",
        "DOCKER_HOST": "unix:///local/forgeflow/docker.sock",
        "DOCKER_CONFIG": "/local/forgeflow/docker-config",
        "DOCKER_CONTEXT": "forgeflow",
    }
    assert dict(os.environ) == process_before


def test_docker_environment_preserves_process_overrides_and_empty_values(tmp_path):
    (tmp_path / ".env").write_text(
        "DOCKER_HOST=unix:///file/docker.sock\n"
        "DOCKER_CONTEXT=file-context\n"
        "DOCKER_CONFIG=/file/config\n",
        encoding="utf-8",
    )
    original = {
        "DOCKER_HOST": "unix:///process/docker.sock",
        "DOCKER_CONTEXT": "",
        "PATH": "/process/bin",
    }
    environment = docker_environment(tmp_path, environ=original)
    assert environment["DOCKER_HOST"] == original["DOCKER_HOST"]
    assert environment["DOCKER_CONTEXT"] == ""
    assert environment["DOCKER_CONFIG"] == "/file/config"
    assert original == {
        "DOCKER_HOST": "unix:///process/docker.sock",
        "DOCKER_CONTEXT": "",
        "PATH": "/process/bin",
    }


def test_docker_environment_does_not_find_parent_files_or_expand_variables(tmp_path):
    (tmp_path / ".env").write_text("DOCKER_HOST=unix:///parent/docker.sock\n", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    assert docker_environment(workspace, environ={}) == {}
    (workspace / ".env").write_text("DOCKER_HOST=${NEBIUS_API_KEY}\n", encoding="utf-8")
    assert docker_environment(workspace, environ={}) == {"DOCKER_HOST": "${NEBIUS_API_KEY}"}


def test_doctor_passes_workspace_docker_configuration_to_read_only_subprocess(
    tmp_path, monkeypatch
):
    (tmp_path / ".env").write_text(
        "DOCKER_HOST=unix:///workspace/forgeflow/docker.sock\n"
        "DOCKER_CONFIG=/workspace/docker-config\n"
        "NEBIUS_API_KEY=local-secret\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "forgeflow.doctor.shutil.which",
        lambda name: "/usr/bin/docker" if name == "docker" else None,
    )
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(command, 1, "", "")

    monkeypatch.setattr("forgeflow.doctor.subprocess.run", fake_run)
    report = run_doctor(tmp_path, environ={"PATH": "/usr/bin"})
    assert len(calls) == 1
    assert calls[0][0][1] == "info"
    assert calls[0][1]["env"] == {
        "PATH": "/usr/bin",
        "DOCKER_HOST": "unix:///workspace/forgeflow/docker.sock",
        "DOCKER_CONFIG": "/workspace/docker-config",
    }
    assert "local-secret" not in json.dumps(report)

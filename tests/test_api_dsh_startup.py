"""Startup must separate mutable assistant state from read-only recorded runs."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings


def _settings(tmp_path: Path):
    return create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        "FORGEFLOW_SESSION_SECRET": "synthetic-startup-secret-01234567890123456789",
        "FORGEFLOW_INVITE_CODES": "synthetic-startup-invite",
        "FORGEFLOW_SECURE_COOKIE": "false",
    })


@pytest.mark.parametrize("enabled,cli", [("0", "unused-cli"), ("1", "")])
def test_disabled_or_unconfigured_dsh_ignores_unwritable_paths(tmp_path, monkeypatch, enabled, cli):
    blocked = tmp_path / "not-a-directory"
    blocked.write_text("cannot create children here", encoding="utf-8")
    monkeypatch.setenv("FORGEFLOW_DSH_ENABLED", enabled)
    monkeypatch.setenv("FORGEFLOW_DSH_CLI", cli)
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", "unused-patch")
    monkeypatch.setenv("FORGEFLOW_DSH_HOME", str(blocked / "home"))
    monkeypatch.setenv("FORGEFLOW_DSH_WORKSPACE", str(blocked / "workspace"))
    with TestClient(create_app(_settings(tmp_path))) as client:
        assert client.get("/api/health").status_code == 200
        assert client.app.state.dsh_harness is None
    assert blocked.read_text(encoding="utf-8") == "cannot create children here"


@pytest.mark.parametrize("sdk", ["0", "1"])
def test_enabled_dsh_initializes_writable_state_without_writing_recorded_runs(tmp_path, monkeypatch, sdk):
    if sdk == "1":
        pytest.importorskip("deepseek_harness")
    runs = tmp_path / "runs"
    runs.mkdir()
    runs.chmod(0o555)
    runtime = tmp_path / "assistant-state"
    cli, patch = tmp_path / "cli.js", tmp_path / "patch.json"
    cli.write_text("// test CLI, never executed", encoding="utf-8")
    patch.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("FORGEFLOW_DSH_ENABLED", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_SDK", sdk)
    monkeypatch.setenv("FORGEFLOW_DSH_CLI", str(cli))
    monkeypatch.setenv("FORGEFLOW_DSH_PATCH", str(patch))
    monkeypatch.setenv("FORGEFLOW_DSH_HOME", str(runtime / "home"))
    monkeypatch.setenv("FORGEFLOW_DSH_WORKSPACE", str(runtime / "workspace"))
    try:
        with TestClient(create_app(_settings(tmp_path))) as client:
            assert client.get("/api/health").status_code == 200
            harness = client.app.state.dsh_harness
            assert harness.configured()
            assert harness.home.is_dir() and harness.workspace.is_dir()
            (harness.home / "session-probe").write_text("writable", encoding="utf-8")
            (harness.workspace / "upload-probe").write_text("writable", encoding="utf-8")
        assert list(runs.iterdir()) == []
    finally:
        runs.chmod(0o755)

from __future__ import annotations

import json
import os

from forgeflow import cli
from forgeflow.api.runs import default_command


def test_api_command_starts_uvicorn_with_settings(monkeypatch, tmp_path):
    captured = {}

    def fake_run(app, host, port):
        captured.update(host=host, port=port, app=app)

    monkeypatch.setenv("FORGEFLOW_DATA_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("FORGEFLOW_SESSION_SECRET", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("FORGEFLOW_INVITE_CODES", "invite-code-for-tests-0001")
    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main(["api", "--host", "0.0.0.0", "--port", "9000"]) == 0
    assert captured["host"] == "0.0.0.0" and captured["port"] == 9000


def test_api_uses_the_platform_port_when_no_flag(monkeypatch, tmp_path):
    captured = {}

    def fake_run(app, host, port):
        captured.update(host=host, port=port, app=app)

    monkeypatch.setenv("FORGEFLOW_DATA_ROOT", str(tmp_path / "runs"))
    monkeypatch.setenv("FORGEFLOW_SESSION_SECRET", "test-session-secret-0123456789abcdef")
    monkeypatch.setenv("FORGEFLOW_INVITE_CODES", "invite-code-for-tests-0001")
    monkeypatch.setenv("PORT", "9123")
    monkeypatch.setattr("uvicorn.run", fake_run)
    assert cli.main(["api", "--host", "0.0.0.0"]) == 0
    assert captured["port"] == 9123


def test_api_runner_child_can_create_its_run_directory(monkeypatch, tmp_path):
    """Exercise the real CLI child, stopping safely at missing-key preflight."""
    captured = {}

    def capture_app(settings, **kwargs):
        captured.update(kwargs)
        return object()

    # The child gets an empty workspace and no provider credentials. It must
    # create a manifest before failing preflight, without calling Docker/model.
    monkeypatch.setattr(os, "environ", {
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        "FORGEFLOW_SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "FORGEFLOW_INVITE_CODES": "invite-code-for-tests-0001",
    })
    monkeypatch.setattr(cli, "__file__", str(tmp_path / "src/forgeflow/cli.py"))
    monkeypatch.setattr("forgeflow.api.app.create_app", capture_app)
    monkeypatch.setattr("uvicorn.run", lambda *_args, **_kwargs: None)
    assert cli.main(["api", "--runner"]) == 0

    run_id = "api-runner-regression"
    process = captured["spawn"](default_command(run_id, "A"), run_id)
    try:
        assert process.wait(timeout=15) == 1  # Missing model key, not a launch crash.
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

    run_dir = tmp_path / "runs" / run_id
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert "NEBIUS_API_KEY" in manifest["error"]["message"]
    log = tmp_path / "approvals/runner-logs" / f"{run_id}.log"
    assert "FileExistsError" not in log.read_text()
    assert log.stat().st_mode & 0o777 == 0o600

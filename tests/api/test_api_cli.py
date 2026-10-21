from __future__ import annotations

from forgeflow import cli


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

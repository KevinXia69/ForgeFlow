"""The formal runtime entry starts, reports, redacts and stops a service group."""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ROOT / "scripts/forgeflow_runtime.py"


def _run(config: Path, action: str) -> dict:
    import subprocess
    proc = subprocess.run([sys.executable, str(RUNTIME), action, "--config", str(config)],
                          capture_output=True, text=True, check=False, timeout=60)
    return json.loads(proc.stdout)


def _config(tmp_path: Path, *, env: dict | None = None) -> Path:
    path = tmp_path / "runtime.json"
    path.write_text(json.dumps({
        "state_dir": str(tmp_path / "state"),
        "services": {"sleeper": {
            "command": [sys.executable, "-c", "import time; time.sleep(120)"],
            "env": env or {}}},
    }), encoding="utf-8")
    return path


def test_start_status_stop_and_restart(tmp_path: Path) -> None:
    config = _config(tmp_path)
    first = _run(config, "start")
    assert first["services"]["sleeper"]["state"] == "started"
    pid = first["services"]["sleeper"]["pid"]
    again = _run(config, "start")
    assert again["services"]["sleeper"] == {"state": "already_running", "pid": pid}
    status = _run(config, "status")
    assert status["ok"] is True and status["services"]["sleeper"]["pid"] == pid
    stopped = _run(config, "stop")
    assert stopped["services"]["sleeper"]["state"] == "stopped"
    assert _run(config, "status")["services"]["sleeper"]["alive"] is False
    assert _run(config, "stop")["services"]["sleeper"]["state"] == "not_running"


def test_diagnose_redacts_secret_values_and_database_credentials(tmp_path: Path) -> None:
    config = _config(tmp_path, env={
        "API_TOKEN": "super-secret-value",
        "FORGEFLOW_CONTROL_DATABASE_URL": "postgresql://user:pw@db.example/control"})
    _run(config, "start")
    try:
        text = json.dumps(_run(config, "diagnose"))
        assert "super-secret-value" not in text
        assert "user:pw@" not in text
        assert "<set>" in text
    finally:
        _run(config, "stop")

from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings

SECRET = "test-session-secret-0123456789abcdef"
INVITE = "invite-code-for-tests-0001"


def make_run(root: Path) -> Path:
    run_dir = root / "sse-run"
    trial = run_dir / "harbor/job/one"
    (trial / "verifier").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(json.dumps({
        "agent": "pi", "task": "2280_medium_repair_plan_medium",
        "run_id": "sse-run", "state": "completed", "model": {"model": "nvidia/test"},
    }))
    (run_dir / "summary.json").write_text(json.dumps({
        "schema_version": 1, "run_id": "sse-run", "state": "completed",
        "task": "2280_medium_repair_plan_medium", "score": 100.0,
        "evaluation_valid": True, "strict_success": True,
    }))
    (trial / "verifier/reward.json").write_text(json.dumps({
        "overall_score": 100.0, "passed": True,
        "rules": {"by_dimension": {"constraint": [
            {"status": "PASS", "applicable": True, "expr": "check_order_links"}
        ]}},
    }))
    return run_dir


def make_client(root: Path) -> TestClient:
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(root),
        "FORGEFLOW_APPROVALS_ROOT": str(root.parent / "approvals"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_PUBLIC_RUNS": "*",
    })
    return TestClient(create_app(settings), base_url="https://testserver")


def test_events_require_auth_and_a_known_run(tmp_path):
    make_run(tmp_path)
    with make_client(tmp_path) as client:
        assert client.get("/api/runs/sse-run/events").status_code == 401
        client.post("/api/auth/session", json={"invite_code": INVITE})
        assert client.get("/api/runs/missing-run/events").status_code == 404


def test_a_completed_run_streams_one_snapshot_then_end(tmp_path):
    make_run(tmp_path)
    with make_client(tmp_path) as client:
        client.post("/api/auth/session", json={"invite_code": INVITE})
        with client.stream("GET", "/api/runs/sse-run/events") as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            body = "".join(response.iter_text())
    events = [line for line in body.splitlines() if line.startswith("event: ")]
    assert events == ["event: snapshot", "event: end"]
    data = next(line for line in body.splitlines() if line.startswith("data: {"))
    payload = json.loads(data.removeprefix("data: "))
    assert payload["run_id"] == "sse-run"
    assert payload["state"] == "completed"
    assert payload["score"] == 100.0
    assert "evaluation_valid" in payload and "attestation" in payload

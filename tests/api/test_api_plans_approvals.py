from __future__ import annotations

import json
from pathlib import Path

from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings

SECRET = "test-session-secret-0123456789abcdef"
INVITE = "invite-code-for-tests-0001"
PLAN = "c" * 64
POLICY = "a" * 64
SNAPSHOT = "b" * 64


def make_plan(root: Path, *, policy: str | None = POLICY, name: str = "plan-run") -> tuple[Path, str]:
    run_dir = root / name
    plan = run_dir / "harbor/job/one/agent/recovery" / PLAN
    plan.mkdir(parents=True)
    prepared = {
        "ok": True, "odoo_writes": False, "plan_id": PLAN,
        "operation_counts": {"keep": 1, "update": 2, "cancel": 0, "create": 1},
        "step_count": 4, "expected_new_spend": 12.5,
    }
    if policy:
        prepared["policy_sha256"] = policy
    (plan / "prepared.json").write_text(json.dumps(prepared))
    (plan / "manifest.json").write_text(json.dumps({
        "schema_version": 1, "artifacts": {"snapshot": SNAPSHOT},
    }))
    return run_dir, PLAN


def sign_in(client: TestClient) -> str:
    response = client.post("/api/auth/session", json={"invite_code": INVITE})
    assert response.status_code == 201
    return response.json()["uid"]


def make_client(runs_root: Path) -> TestClient:
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(runs_root),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_PUBLIC_RUNS": "*",
    })
    return TestClient(create_app(settings), base_url="https://testserver")


def test_plans_and_approval_round_trip(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    make_plan(runs_root)
    with make_client(runs_root) as client:
        assert client.get(f"/api/runs/plan-run/plans/{PLAN}").status_code == 401
        uid = sign_in(client)
        listed = client.get("/api/runs/plan-run/plans")
        assert listed.status_code == 200
        assert [item["plan_id"] for item in listed.json()] == [PLAN]
        assert listed.json()[0]["state"] == "prepared"
        detail = client.get(f"/api/runs/plan-run/plans/{PLAN}").json()
        assert detail["policy_sha256"] == POLICY
        assert detail["snapshot_sha256"] == SNAPSHOT

        approved = client.post(f"/api/runs/plan-run/plans/{PLAN}/approvals", json={})
        assert approved.status_code == 409
        assert approved.json()["detail"] == "historical_evidence_read_only"
        assert client.get(f"/api/runs/plan-run/plans/{PLAN}/approvals").json() == []
        assert uid


def test_a_plan_without_a_recorded_policy_cannot_be_approved(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    make_plan(runs_root, policy=None, name="no-policy")
    with make_client(runs_root) as client:
        sign_in(client)
        response = client.post(f"/api/runs/no-policy/plans/{PLAN}/approvals", json={})
        assert response.status_code == 409
        assert response.json()["detail"] == "historical_evidence_read_only"


def test_unknown_plan_and_bad_ids_are_not_found(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    make_plan(runs_root)
    with make_client(runs_root) as client:
        sign_in(client)
        assert client.get("/api/runs/plan-run/plans/" + "d" * 64).status_code == 404
        assert client.get("/api/runs/plan-run/plans/not-a-plan").status_code == 404
        assert client.get("/api/runs/plan-run/plans/..%2F..%2Fetc").status_code in (400, 404)


def test_historical_approval_rejection_issues_no_grant(tmp_path):
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    make_plan(runs_root)
    with make_client(runs_root) as client:
        sign_in(client)
        result = client.post(f"/api/runs/plan-run/plans/{PLAN}/approvals", json={})
        assert result.status_code == 409
        result = client.post(f"/api/runs/plan-run/plans/{PLAN}/approvals/{'a' * 32}/revoke")
        assert result.status_code == 409
    assert not (tmp_path / "approvals" / "audit.jsonl").exists()

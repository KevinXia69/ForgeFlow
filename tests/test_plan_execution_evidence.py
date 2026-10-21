from __future__ import annotations

import hashlib
import json

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.plans import list_plans, read_plan
from forgeflow.api.settings import create_settings
from forgeflow.runview import recovery_view

PLAN = "a" * 64


def artifacts(tmp_path, events=(), success=None):
    root = tmp_path / "evidence"
    run = root / "sample"
    trial = run / "harbor/job/one"
    directory = trial / "agent/recovery" / PLAN
    directory.mkdir(parents=True)
    (directory / "prepared.json").write_text(json.dumps({
        "ok": True, "plan_id": PLAN, "odoo_writes": False, "step_count": 2,
        "operation_counts": {"keep": 0, "update": 2, "cancel": 0, "create": 0},
    }))
    if events is not None:
        previous = "0" * 64
        rows = []
        for index, (event, data) in enumerate(events):
            row = {"index": index, "previous": previous, "event": event, "data": data}
            previous = hashlib.sha256(json.dumps(
                row, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()).hexdigest()
            rows.append({**row, "sha256": previous})
        (directory / "journal.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
    if success is not None:
        (directory / "success.json").write_text(json.dumps(success))
    return root, run, trial, directory


HEADER = ("plan", {"schema_version": 1, "plan_sha256": "b" * 64, "step_count": 2})
D1 = ("dispatch", {"key": "step:1", "step_sha256": "c" * 64})
V1 = ("verified", D1[1])
D2 = ("dispatch", {"key": "step:2", "step_sha256": "d" * 64})
V2 = ("verified", D2[1])


def both(root, run, trial, directory):
    before = {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    plan = read_plan(run, PLAN)
    recovery = recovery_view(run, trial, root)
    assert plan["execution"] == recovery["execution"]
    assert list_plans(run)[0]["execution"] == plan["execution"]
    assert before == {path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()}
    return plan, recovery


@pytest.mark.parametrize("events,journal_status", [(None, "missing"), ([], "empty"), ([HEADER], "plan_only")])
def test_prepared_has_no_execution_result_not_a_claim_of_no_writes(tmp_path, events, journal_status):
    plan, recovery = both(*artifacts(tmp_path, events))
    assert plan["state"] == recovery["status"] == "prepared"
    assert plan["execution"]["status"] == "not_recorded"
    assert plan["execution"]["journal_status"] == journal_status
    assert recovery["odoo_writes_executed"] is None
    assert recovery["execution_validated"] is None


@pytest.mark.parametrize("events,dispatches,verified,pending", [
    ([HEADER, D1], 1, 0, True),
    ([HEADER, D1, V1, D2], 2, 1, True),
    ([HEADER, D1, V1, D2, V2], 2, 2, False),
    ([HEADER, ("already_applied", D1[1])], 0, 1, False),
])
def test_journal_prefix_without_success_is_unknown(tmp_path, events, dispatches, verified, pending):
    plan, recovery = both(*artifacts(tmp_path, events))
    assert plan["state"] == recovery["status"] == "unknown"
    execution = plan["execution"]
    assert execution["status"] == "unknown"
    assert execution["reason"] == "journal_without_success"
    assert execution["dispatch_count"] == dispatches
    assert execution["verified_count"] == verified
    assert execution["pending_dispatch"] is pending
    assert recovery["odoo_writes_executed"] is None
    assert recovery["verified_steps"] is None
    assert recovery["execution_validated"] is None


@pytest.mark.parametrize("bad", ["truncated", "hash", "transition", "header_count", "array", "json", "symlink"])
def test_bad_journal_never_claims_prepared_only_success_or_known_counts(tmp_path, bad):
    values = artifacts(tmp_path, [HEADER, D1])
    directory = values[3]
    journal = directory / "journal.jsonl"
    if bad == "truncated":
        journal.write_text(journal.read_text().rstrip("\n"))
    elif bad == "hash":
        journal.write_text(journal.read_text().replace('"sha256": "', '"sha256": "0', 1))
    elif bad == "transition":
        values = artifacts(tmp_path / "transition", [HEADER, V1])
    elif bad == "header_count":
        values = artifacts(tmp_path / "count", [("plan", {**HEADER[1], "step_count": 3})])
    elif bad in {"array", "json"}:
        journal.write_text("[]\n" if bad == "array" else "private-fixture-canary\n")
    else:
        journal.unlink()
        target = tmp_path / "private-canary"
        target.write_text("private-fixture-canary")
        journal.symlink_to(target)
    plan = read_plan(values[1], PLAN)
    recovery = recovery_view(values[1], values[2], values[0])
    assert plan["state"] == recovery["status"] == "unknown"
    assert plan["execution"]["dispatch_count"] is None
    assert plan["execution"]["verified_count"] is None
    assert "private-fixture-canary" not in repr(plan)


def test_complete_success_remains_execution_success_not_business_verification(tmp_path):
    success = {"ok": True, "plan_id": PLAN, "execution_validated": True,
               "verified_steps": 2, "odoo_writes": True, "benchmark_success": None}
    plan, recovery = both(*artifacts(tmp_path, [HEADER, D1, V1, D2, V2], success))
    assert plan["state"] == "succeeded"
    assert recovery["status"] == "executed"
    assert plan["success"] == success
    assert recovery["execution_validated"] is True
    assert plan["success"]["benchmark_success"] is None


@pytest.mark.parametrize("success,expected", [
    ({"ok": False, "plan_id": PLAN, "execution_validated": False}, "stopped"),
    ({"ok": True, "plan_id": PLAN, "execution_validated": True, "verified_steps": 1}, "unknown"),
    ({"ok": True, "plan_id": "f" * 64, "execution_validated": True, "verified_steps": 2}, "unknown"),
])
def test_noncomplete_or_different_success_does_not_hide_uncertainty(tmp_path, success, expected):
    root, run, trial, directory = artifacts(tmp_path, [HEADER, D1], success)
    plan = read_plan(run, PLAN)
    recovery = recovery_view(run, trial, root)
    assert plan["state"] == recovery["status"] == expected
    assert plan["success"] == success
    assert recovery["execution_validated"] is not True


def test_symlinked_artifact_parent_is_not_read(tmp_path):
    root, run, trial, directory = artifacts(tmp_path, [HEADER, D1])
    outside = tmp_path / "outside"
    (trial / "agent").rename(outside)
    (trial / "agent").symlink_to(outside, target_is_directory=True)
    assert read_plan(run, PLAN) is None
    assert list_plans(run) == []


def test_authenticated_plan_api_projects_partial_journal_and_stays_read_only(tmp_path):
    root, run, trial, directory = artifacts(tmp_path, [HEADER, D1, V1, D2])
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(root), "FORGEFLOW_PUBLIC_RUNS": "*",
        "FORGEFLOW_SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "FORGEFLOW_INVITE_CODES": "test-invite-code-0123456789",
        "FORGEFLOW_SECURE_COOKIE": "true",
    })
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        assert client.get(f"/api/runs/sample/plans/{PLAN}").status_code == 401
        assert client.post("/api/auth/session", json={"invite_code": "test-invite-code-0123456789"}).status_code == 201
        detail = client.get(f"/api/runs/sample/plans/{PLAN}").json()
        assert detail["state"] == "unknown"
        assert detail["execution"]["dispatch_count"] == 2
        assert detail["execution"]["verified_count"] == 1
        listed = client.get("/api/runs/sample/plans").json()
        assert listed[0]["execution"] == detail["execution"]
        assert client.post(f"/api/runs/sample/plans/{PLAN}/approvals", json={}).status_code == 409


def test_six_dispatches_five_verifications_remain_unknown(tmp_path):
    events = [("plan", {**HEADER[1], "step_count": 6})]
    for number in range(6):
        data = {"key": f"step:{number}", "step_sha256": "c" * 64}
        events.append(("dispatch", data))
        if number < 5:
            events.append(("verified", data))
    root, run, trial, directory = artifacts(tmp_path, events)
    prepared = directory / "prepared.json"
    value = json.loads(prepared.read_text())
    prepared.write_text(json.dumps({**value, "step_count": 6}))
    plan, recovery = both(root, run, trial, directory)
    assert plan["state"] == recovery["status"] == "unknown"
    assert plan["execution"]["dispatch_count"] == 6
    assert plan["execution"]["verified_count"] == 5
    assert plan["execution"]["pending_dispatch"] is True
    assert recovery["odoo_writes_executed"] is None


@pytest.mark.parametrize("events", [[HEADER, D1, V1, D2], [HEADER]])
def test_success_claim_cannot_override_contradictory_journal(tmp_path, events):
    success = {"ok": True, "plan_id": PLAN, "execution_validated": True,
               "verified_steps": 2, "odoo_writes": True}
    plan, recovery = both(*artifacts(tmp_path, events, success))
    assert plan["state"] == recovery["status"] == "unknown"
    assert plan["execution"]["reason"] == "success_journal_conflict"
    assert plan["success"] == success
    assert recovery["execution_validated"] is None


def test_complete_legacy_success_without_journal_still_remains_explicit(tmp_path):
    success = {"ok": True, "plan_id": PLAN, "execution_validated": True,
               "verified_steps": 2, "odoo_writes": True}
    plan, recovery = both(*artifacts(tmp_path, None, success))
    assert plan["state"] == "succeeded"
    assert recovery["status"] == "executed"
    assert plan["execution"]["journal_status"] == "missing"
    assert plan["execution"]["dispatch_count"] is None

"""A restored backup pauses business writes until an admin verifies the environment."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.restore import RestoreGuard
from forgeflow.api.settings import create_settings

SECRET = "restore-test-session-secret-0123456789"
INVITE = "restore-test-invite-0001"


def _client(tmp_path: Path, *, expected: dict | None = None) -> tuple[TestClient, RestoreGuard]:
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_SECURE_COOKIE": "false",
    })
    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    guard.pause(reason="restored_backup", expected=expected or {})
    return TestClient(create_app(settings, restore_guard=guard)), guard


def test_restore_marker_blocks_business_writes_but_allows_login(tmp_path: Path) -> None:
    client, guard = _client(tmp_path)
    with client:
        assert client.post("/api/knowledge/answer", json={"question": "x"}).status_code == 503
        assert client.post("/api/auth/session", json={"invite_code": INVITE}).status_code == 201
        status = client.get("/api/restore/status")
        assert status.status_code == 200
        assert status.json()["paused"] is True
        verified = client.post("/api/restore/verify")
        assert verified.status_code == 200
        assert verified.json()["verified"] is True
        assert not guard.marker.exists()
        assert client.post("/api/knowledge/answer",
                           json={"question": "x"}).status_code != 503


def test_a_mismatched_environment_keeps_the_restore_pause(tmp_path: Path) -> None:
    client, guard = _client(tmp_path, expected={"control_db": "expected_db",
                                                "migrations_max": 999})
    with client:
        client.post("/api/auth/session", json={"invite_code": INVITE})
        response = client.post("/api/restore/verify")
        assert response.status_code == 409
        assert response.json()["detail"]["code"] == "restore_environment_mismatch"
        assert guard.marker.exists()
        assert client.get("/api/restore/status").json()["paused"] is True


def test_operation_fingerprint_mismatch_keeps_the_pause(tmp_path: Path) -> None:
    from forgeflow.api.restore import RestoreError, RestoreGuard

    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    guard.pause(reason="restored_backup", expected={
        "control_db": "forgeflow_control", "migrations_max": 18,
        "last_effect_id": "effect-expected", "last_ticket_id": "ticket-expected"})
    with pytest.raises(RestoreError) as error:
        guard.verify({"control_db": "forgeflow_control", "migrations_max": 18,
                      "last_effect_id": "effect-other",
                      "last_ticket_id": "ticket-expected"})
    assert error.value.mismatched == ["last_effect_id"]
    assert guard.marker.exists()
    assert guard.verify({"control_db": "forgeflow_control", "migrations_max": 18,
                         "last_effect_id": "effect-expected",
                         "last_ticket_id": "ticket-expected"})["verified"] is True


def test_current_fingerprint_reads_the_real_control_database() -> None:
    import os

    if not os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL"):
        pytest.skip("FORGEFLOW_CONTROL_DATABASE_URL not set")
    from forgeflow.api.restore import current_fingerprint
    fingerprint = current_fingerprint()
    assert fingerprint.get("control_db")
    assert isinstance(fingerprint.get("migrations_max"), int)


def test_restore_verify_requires_an_admin_account(tmp_path: Path) -> None:
    from forgeflow.accounts import AccountStore, totp
    from forgeflow.api.restore import RestoreGuard

    accounts = AccountStore(tmp_path / "accounts.sqlite3")
    admin, secret = accounts.bootstrap(email="admin@example.com",
                                       password="correct-horse-1",
                                       tenant_id="default")
    code = accounts.invite(admin_principal_id=admin.principal_id,
                           email="planner@example.com", role="planner")
    accounts.activate(code=code, password="planner-pass-1")
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_SECURE_COOKIE": "false",
    })
    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    guard.pause(reason="restored_backup", expected={})
    with TestClient(create_app(settings, accounts=accounts,
                               restore_guard=guard)) as client:
        planner = client.post("/api/accounts/login", json={
            "email": "planner@example.com", "password": "planner-pass-1"})
        assert planner.status_code == 200
        denied = client.post("/api/restore/verify")
        assert denied.status_code == 403
        assert denied.json()["detail"] == "restore_admin_required"
        assert guard.marker.exists()

        login = client.post("/api/accounts/login", json={
            "email": "admin@example.com", "password": "correct-horse-1"})
        assert login.status_code == 200 and login.json()["mfa_required"] is True
        verified = client.post("/api/accounts/mfa", json={
            "challenge": login.json()["challenge"], "code": totp(secret)})
        assert verified.status_code == 200
        allowed = client.post("/api/restore/verify")
        assert allowed.status_code == 200 and allowed.json()["verified"] is True
        assert not guard.marker.exists()


def _operation(**overrides):
    values = {
        "effect_id": "effect-1", "ticket_id": "ticket-1",
        "kind": "apply_approved_plan", "state": "pending",
        "side_effect_class": "business_write",
        "idempotency_key": "apply:77:abc", "payload_sha256": "a" * 64,
    }
    values.update(overrides)
    return values


def test_operations_are_checked_field_by_field_after_a_restore(tmp_path: Path) -> None:
    from forgeflow.api.restore import RestoreError, RestoreGuard

    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    expected = _operation()
    guard.pause(reason="restored_backup", expected={}, operations=[expected])
    assert guard.verify({}, operations=[expected])["operations_checked"] == 1
    assert not guard.marker.exists()

    guard.pause(reason="restored_backup", expected={}, operations=[expected])
    with pytest.raises(RestoreError) as changed:
        guard.verify({}, operations=[_operation(payload_sha256="b" * 64)])
    assert changed.value.mismatched == ["operation:effect-1:payload_sha256"]
    assert guard.marker.exists()

    with pytest.raises(RestoreError) as missing:
        guard.verify({}, operations=[])
    assert missing.value.mismatched == ["operation:effect-1:missing"]


def test_a_new_unresolved_business_write_keeps_the_pause(tmp_path: Path) -> None:
    from forgeflow.api.restore import RestoreError, RestoreGuard

    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    expected = _operation()
    guard.pause(reason="restored_backup", expected={}, operations=[expected])
    with pytest.raises(RestoreError) as unexpected:
        guard.verify({}, operations=[expected, _operation(effect_id="effect-2")])
    assert unexpected.value.mismatched == ["operation:effect-2:unexpected"]


def test_a_pause_without_an_operation_snapshot_is_not_silently_verified(
        tmp_path: Path) -> None:
    from forgeflow.api.restore import RestoreError, RestoreGuard

    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    guard.pause(reason="restored_backup", expected={},
                operations=[_operation()])
    with pytest.raises(RestoreError) as unavailable:
        guard.verify({})
    assert unavailable.value.mismatched == ["operations"]


def test_the_api_verifies_captured_operations(tmp_path: Path, monkeypatch) -> None:
    import forgeflow.api.restore as restore_module

    operation = _operation()
    monkeypatch.setattr(restore_module, "current_operations",
                        lambda *args, **kwargs: [operation])
    client, guard = _client(tmp_path)
    guard.pause(reason="restored_backup", expected={}, operations=[operation])
    with client:
        client.post("/api/auth/session", json={"invite_code": INVITE})
        verified = client.post("/api/restore/verify")
        assert verified.status_code == 200, verified.text
        assert verified.json()["operations_checked"] == 1
        assert not guard.marker.exists()


def test_a_corrupt_marker_is_never_verified_away(tmp_path: Path) -> None:
    from forgeflow.api.restore import RestoreError, RestoreGuard

    marker = tmp_path / "RESTORE_REQUIRED"
    marker.write_text("{not json", encoding="utf-8")
    guard = RestoreGuard(marker)
    with pytest.raises(RestoreError) as unreadable:
        guard.verify({})
    assert unreadable.value.code == "restore_marker_unreadable"
    assert marker.exists()
    marker.write_text('{"expected": {}, "operations": []}', encoding="utf-8")
    with pytest.raises(RestoreError) as invalid:
        guard.verify({})
    assert invalid.value.code == "restore_marker_unreadable"
    assert marker.exists()


def test_an_unreachable_control_database_keeps_the_pause(tmp_path: Path) -> None:
    from forgeflow.api.restore import RestoreError, RestoreGuard

    guard = RestoreGuard(tmp_path / "RESTORE_REQUIRED")
    guard.pause(reason="restored_backup", expected={})
    with pytest.raises(RestoreError) as unavailable:
        guard.verify({"error": "control_database_unreachable"})
    assert unavailable.value.code == "restore_environment_unavailable"
    assert guard.marker.exists()


def test_the_script_and_api_share_the_data_root_default(tmp_path: Path) -> None:
    import os
    import subprocess
    import sys

    from forgeflow.api.restore import default_restore_marker

    root = tmp_path / "data"
    repo = Path(__file__).resolve().parents[2]
    env = {**os.environ, "FORGEFLOW_DATA_ROOT": str(root)}
    env.pop("FORGEFLOW_RESTORE_MARKER", None)
    completed = subprocess.run(
        [sys.executable, str(repo / "scripts/restore_pause.py"), "pause"],
        capture_output=True, text=True, env=env, cwd=repo)
    assert completed.returncode == 0, completed.stderr
    expected = root / "RESTORE_REQUIRED"
    assert expected.is_file()
    assert default_restore_marker(root).resolve() == expected.resolve()
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(root),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_SECURE_COOKIE": "false",
    })
    client = TestClient(create_app(settings))
    with client:
        client.post("/api/auth/session", json={"invite_code": INVITE})
        status = client.get("/api/restore/status")
        assert status.status_code == 200
        assert status.json()["paused"] is True

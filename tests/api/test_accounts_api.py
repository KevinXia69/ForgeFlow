"""Account API: invite activation, MFA, role checks and session revocation."""
from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.accounts import AccountStore, totp
from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings


@pytest.fixture
def account_settings(tmp_path: Path):
    # Secure cookies are not sent over the TestClient's http scheme; production
    # keeps secure_cookie=true, the test uses the local setting.
    return create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
        "FORGEFLOW_SESSION_SECRET": "account-test-session-secret-0123456789",
        "FORGEFLOW_INVITE_CODES": "account-test-invite-0001",
        "FORGEFLOW_SECURE_COOKIE": "false",
        "FORGEFLOW_PUBLIC_RUNS": "*",
    })


@pytest.fixture
def accounts(tmp_path: Path, monkeypatch) -> AccountStore:
    monkeypatch.setenv("FORGEFLOW_ACCOUNTS_BOOTSTRAP_TOKEN", "bootstrap-token-1")
    return AccountStore(tmp_path / "accounts.sqlite3")


@pytest.fixture
def app_client(account_settings, accounts):
    with TestClient(create_app(account_settings, accounts=accounts)) as client:
        yield client


def _bootstrap(client, accounts, *, allow_existing: bool = False):
    response = client.post("/api/accounts/bootstrap", json={
        "token": "bootstrap-token-1", "email": "admin@example.com",
        "password": "correct-horse-1", "tenant_id": "tenant-a"})
    if allow_existing and response.status_code == 409:
        return None
    assert response.status_code == 201, response.text
    return response.json()


def _admin_session(client, accounts):
    _bootstrap(client, accounts, allow_existing=True)
    login = client.post("/api/accounts/login", json={
        "email": "admin@example.com", "password": "correct-horse-1"})
    assert login.status_code == 200 and login.json()["mfa_required"] is True
    secret = accounts._connection.execute(
        "SELECT mfa_secret FROM accounts WHERE email='admin@example.com'"
    ).fetchone()["mfa_secret"]
    verified = client.post("/api/accounts/mfa", json={
        "challenge": login.json()["challenge"], "code": totp(secret)})
    assert verified.status_code == 200, verified.text
    return verified.json()


def test_bootstrap_login_mfa_and_me(app_client, accounts) -> None:
    session = _admin_session(app_client, accounts)
    assert session["account"]["role"] == "admin"
    me = app_client.get("/api/accounts/me")
    assert me.status_code == 200
    assert me.json()["email"] == "admin@example.com"
    assert me.json()["mfa_enabled"] is True


def test_the_invite_cookie_no_longer_authenticates_when_accounts_exist(
        app_client, account_settings, accounts) -> None:
    legacy = app_client.post("/api/auth/session",
                             json={"invite_code": account_settings.invite_codes[0]})
    # The legacy entry point is closed when accounts are enabled: an invite is an
    # enrolment secret, not a login identity.
    assert legacy.status_code == 403
    assert legacy.json()["detail"] == "accounts_login_required"
    assert app_client.get("/api/accounts/me").status_code == 401


def test_member_management_role_check_and_disable(account_settings, accounts) -> None:
    with TestClient(create_app(account_settings, accounts=accounts)) as admin:
        _admin_session(admin, accounts)
        invited = admin.post("/api/accounts/invites", json={
            "email": "planner@example.com", "role": "planner"})
        assert invited.status_code == 201, invited.text
        code = invited.json()["code"]

    with TestClient(create_app(account_settings, accounts=accounts)) as planner:
        activated = planner.post("/api/accounts/activate", json={
            "code": code, "password": "planner-pass-1"})
        assert activated.status_code == 201, activated.text
        login = planner.post("/api/accounts/login", json={
            "email": "planner@example.com", "password": "planner-pass-1"})
        assert login.status_code == 200 and login.json()["mfa_required"] is False
        assert planner.get("/api/accounts/members").status_code == 403

    with TestClient(create_app(account_settings, accounts=accounts)) as admin:
        _admin_session(admin, accounts)
        members = admin.get("/api/accounts/members")
        assert members.status_code == 200
        planner_row = next(row for row in members.json()
                           if row["email"] == "planner@example.com")
        disabled = admin.post(
            f"/api/accounts/members/{planner_row['id']}/disable")
        assert disabled.status_code == 200
        assert disabled.json()["revoked_sessions"] >= 1

    # The planner's existing session is invalid on the very next request.
    with TestClient(create_app(account_settings, accounts=accounts)) as planner:
        planner.cookies.set("forgeflow_account", "stolen")
        assert planner.get("/api/accounts/me").status_code == 401


def test_admin_cannot_promote_without_an_mfa_secret(account_settings, accounts) -> None:
    with TestClient(create_app(account_settings, accounts=accounts)) as admin:
        _admin_session(admin, accounts)
        invited = admin.post("/api/accounts/invites", json={
            "email": "p2@example.com", "role": "planner"})
        code = invited.json()["code"]
        member_id = None
        activated = admin.post("/api/accounts/activate", json={
            "code": code, "password": "planner-pass-2"})
        assert activated.status_code == 201
        member_id = activated.json()["account"]["id"]
        blocked = admin.post(f"/api/accounts/members/{member_id}/role",
                             json={"role": "approver"})
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "mfa_enrollment_required"
        secret = admin.post(f"/api/accounts/members/{member_id}/mfa")
        assert secret.status_code == 200 and secret.json()["mfa_secret"]
        promoted = admin.post(f"/api/accounts/members/{member_id}/role",
                              json={"role": "approver"})
        assert promoted.status_code == 200
        assert promoted.json()["role"] == "approver"


def test_logout_revokes_the_session(app_client, accounts) -> None:
    _admin_session(app_client, accounts)
    assert app_client.delete("/api/accounts/session").status_code == 204
    assert app_client.get("/api/accounts/me").status_code == 401


def test_public_config_reports_whether_accounts_are_required(app_client) -> None:
    response = app_client.get("/api/config")
    assert response.status_code == 200
    assert response.json()["accounts_enabled"] is True


def test_config_without_an_account_store_keeps_the_invite_login(client) -> None:
    assert client.get("/api/config").json()["accounts_enabled"] is False

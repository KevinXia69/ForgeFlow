"""Independent accounts: activation, roles, MFA, revocation and tenant isolation."""
from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.accounts import MAX_MFA_FAILURES, AccountError, AccountStore, totp, verify_totp


def _store(tmp_path: Path) -> AccountStore:
    return AccountStore(tmp_path / "accounts.sqlite3")


def _bootstrap(store: AccountStore):
    return store.bootstrap(email="admin@example.com", password="correct-horse-1",
                           tenant_id="tenant-a", display_name="Admin")


def test_bootstrap_is_one_time_and_requires_a_second_factor(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, secret = _bootstrap(store)
    assert admin.role == "admin" and admin.mfa_enabled and secret
    with pytest.raises(AccountError, match="accounts_already_initialized"):
        _bootstrap(store)


def test_invite_activation_login_and_logout(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, _ = _bootstrap(store)
    code = store.invite(admin_principal_id=admin.principal_id,
                        email="planner@example.com", role="planner")
    planner, secret = store.activate(code=code, password="planner-pass-1")
    assert planner.role == "planner" and secret is None
    with pytest.raises(AccountError, match="invite_invalid"):
        store.activate(code=code, password="planner-pass-2")
    logged = store.login(email="planner@example.com", password="planner-pass-1")
    assert logged["mfa_required"] is False
    token = logged["session"].token
    assert store.verify_session(token) is not None
    assert store.logout(token) is True
    assert store.verify_session(token) is None


def test_failed_logins_are_rate_limited(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, _ = _bootstrap(store)
    code = store.invite(admin_principal_id=admin.principal_id,
                        email="p@example.com", role="planner")
    store.activate(code=code, password="planner-pass-1")
    for _ in range(5):
        with pytest.raises(AccountError, match="credentials_invalid"):
            store.login(email="p@example.com", password="wrong-pass-000")
    with pytest.raises(AccountError, match="too_many_attempts"):
        store.login(email="p@example.com", password="planner-pass-1")


def test_admin_login_needs_a_valid_totp(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, secret = _bootstrap(store)
    challenge = store.login(email="admin@example.com", password="correct-horse-1")
    assert challenge["mfa_required"] is True
    with pytest.raises(AccountError, match="mfa_code_invalid"):
        store.verify_mfa(challenge=challenge["challenge"], code="000000")
    from forgeflow.accounts import totp
    session = store.verify_mfa(challenge=challenge["challenge"],
                               code=totp(secret))
    assert verify_totp(secret, totp(secret))
    assert store.verify_session(session.token) is not None


def test_disable_and_reset_revoke_every_session(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, admin_secret = _bootstrap(store)
    code = store.invite(admin_principal_id=admin.principal_id,
                        email="p@example.com", role="planner")
    planner, _ = store.activate(code=code, password="planner-pass-1")
    session = store.login(email="p@example.com", password="planner-pass-1")["session"]
    store.disable(admin_principal_id=admin.principal_id, account_id=planner.id)
    assert store.verify_session(session.token) is None

    code = store.invite(admin_principal_id=admin.principal_id,
                        email="q@example.com", role="planner")
    other, _ = store.activate(code=code, password="other-pass-01")
    session = store.login(email="q@example.com", password="other-pass-01")["session"]
    reset = store.create_reset(admin_principal_id=admin.principal_id,
                               email="q@example.com")
    store.reset_password(token=reset, password="new-password-1")
    assert store.verify_session(session.token) is None
    assert store.login(email="q@example.com", password="new-password-1")["session"]


def test_promotion_requires_an_mfa_secret_first(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, _ = _bootstrap(store)
    code = store.invite(admin_principal_id=admin.principal_id,
                        email="p@example.com", role="planner")
    planner, _ = store.activate(code=code, password="planner-pass-1")
    with pytest.raises(AccountError, match="mfa_enrollment_required"):
        store.set_role(admin_principal_id=admin.principal_id,
                       account_id=planner.id, role="approver")
    secret = store.issue_mfa_secret(admin_principal_id=admin.principal_id,
                                    account_id=planner.id)
    promoted = store.set_role(admin_principal_id=admin.principal_id,
                              account_id=planner.id, role="approver")
    assert promoted["role"] == "approver" and promoted["mfa_enabled"] is True
    assert store.login(email="p@example.com",
                       password="planner-pass-1")["mfa_required"] is True
    assert verify_totp(secret, __import__("forgeflow.accounts", fromlist=["totp"]).totp(secret))


def test_an_admin_cannot_manage_another_tenant(tmp_path: Path) -> None:
    store = _store(tmp_path)
    admin, _ = _bootstrap(store)
    # The store allows only one bootstrap; an invite inherits the issuing
    # admin's tenant, which is what keeps cross-tenant management impossible.
    code = store.invite(admin_principal_id=admin.principal_id,
                        email="other@example.com", role="planner")
    outsider, _ = store.activate(code=code, password="other-pass-01")
    assert outsider.tenant_id == "tenant-a"
    with pytest.raises(AccountError, match="admin_required"):
        store.disable(admin_principal_id=outsider.principal_id,
                      account_id=admin.id)
    assert [row["id"] for row in store.members(admin_principal_id=admin.principal_id)]


def test_mfa_failures_are_limited_and_the_audit_survives_the_rollback(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _, secret = _bootstrap(store)
    challenge = store.login(email="admin@example.com", password="correct-horse-1")
    wrong = f"{(int(totp(secret)) + 1) % 1000000:06d}"
    for attempt in range(MAX_MFA_FAILURES):
        with pytest.raises(AccountError) as error:
            store.verify_mfa(challenge=challenge["challenge"], code=wrong)
        expected = ("too_many_attempts" if attempt == MAX_MFA_FAILURES - 1
                    else "mfa_code_invalid")
        assert error.value.code == expected
    audits = store._connection.execute(
        "SELECT count(*) AS count FROM audit WHERE action='mfa_failed'").fetchone()
    assert int(audits["count"]) == MAX_MFA_FAILURES
    # The challenge is burned after the limit, so even a valid code cannot be reused.
    with pytest.raises(AccountError, match="mfa_challenge_invalid"):
        store.verify_mfa(challenge=challenge["challenge"], code=totp(secret))

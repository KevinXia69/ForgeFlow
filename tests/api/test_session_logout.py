"""Full HTTP routing for invite-session revocation and failure boundaries."""
from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.auth import (
    InviteSessionRevocations,
    _decode,
    _encode,
    _session_token_hash,
    _signature,
    issue_cookie,
    verify_cookie,
)


def login(client, settings):
    response = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[0]})
    assert response.status_code == 201
    return response.cookies[settings.cookie_name]


def test_invite_logout_blocks_copied_cookie_across_restart(settings):
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        token = login(client, settings)
        assert verify_cookie(settings, token + "=", audience="testserver") is not None
        assert client.get("/api/auth/me").status_code == 200
        response = client.delete("/api/auth/session")
        assert response.status_code == 204
        assert client.get("/api/auth/me").status_code == 401
        assert client.delete("/api/auth/session").status_code == 204
    with TestClient(create_app(settings), base_url="https://testserver") as restarted:
        restarted.cookies.set(settings.cookie_name, token)
        assert restarted.get("/api/auth/me").status_code == 401
        restarted.cookies.set(settings.cookie_name, token + "=")
        assert restarted.get("/api/auth/me").status_code == 401, "base64-equivalent signature cannot evade revocation"
        fresh = login(restarted, settings)
        assert fresh != token
        assert restarted.get("/api/auth/me").status_code == 200


def test_logout_revokes_only_the_current_session(settings):
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as first, \
            TestClient(app, base_url="https://testserver") as second:
        assert login(first, settings) != login(second, settings)
        assert first.delete("/api/auth/session").status_code == 204
        assert first.get("/api/auth/me").status_code == 401
        assert second.get("/api/auth/me").status_code == 200


def test_stale_workspace_cannot_logout_the_replacement_invite(settings):
    settings = replace(settings, invite_codes=(*settings.invite_codes, "synthetic-other-invite-0001"))
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as client:
        old_cookie = login(client, settings)
        old_scope = client.get("/api/auth/me").json()["scope_id"]
        changed = client.post("/api/auth/session", json={"invite_code": settings.invite_codes[1]})
        new_cookie = changed.cookies[settings.cookie_name]
        new_scope = client.get("/api/auth/me").json()["scope_id"]
        assert new_scope != old_scope
        denied = client.delete("/api/auth/session", headers={"X-ForgeFlow-Workspace": old_scope})
        assert denied.status_code == 409
        assert denied.headers["X-ForgeFlow-Workspace-Changed"] == "1"
        assert "set-cookie" not in denied.headers
        assert not app.state.invite_revocations.revoked(old_cookie)
        assert not app.state.invite_revocations.revoked(new_cookie)
        assert client.get("/api/auth/me").json()["scope_id"] == new_scope
        assert client.delete("/api/auth/session", headers={"X-ForgeFlow-Workspace": new_scope}).status_code == 204
        assert app.state.invite_revocations.revoked(new_cookie)
        # Repeated/expired identity can still clear cookies even with an old hint.
        assert client.delete("/api/auth/session", headers={"X-ForgeFlow-Workspace": old_scope}).status_code == 204


def test_logout_can_revoke_pre_upgrade_signed_cookie_without_sid(settings):
    token = issue_cookie(settings, settings.invite_codes[0], audience="testserver")
    claims = json.loads(_decode(token.split(".")[0]))
    del claims["sid"]
    payload = _encode(json.dumps(claims, separators=(",", ":")).encode())
    legacy = payload + "." + _encode(_signature(settings, payload))
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        client.cookies.set(settings.cookie_name, legacy)
        assert client.get("/api/auth/me").status_code == 200
        assert client.delete("/api/auth/session").status_code == 204
        client.cookies.set(settings.cookie_name, legacy)
        assert client.get("/api/auth/me").status_code == 401


def test_failed_revocation_does_not_confirm_logout_or_delete_cookies(settings, monkeypatch):
    app = create_app(settings)
    with TestClient(app, base_url="https://testserver") as client:
        login(client, settings)

        def unavailable(*args, **kwargs):
            raise sqlite3.OperationalError("synthetic unavailable store")

        monkeypatch.setattr(app.state.invite_revocations, "revoke", unavailable)
        response = client.delete("/api/auth/session")
        assert response.status_code == 503
        assert response.json()["detail"] == "session_revocation_unavailable"
        assert "set-cookie" not in response.headers
        assert client.get("/api/auth/me").status_code == 200
        monkeypatch.setattr(app.state.invite_revocations, "revoked", unavailable)
        assert client.get("/api/auth/me").status_code == 401


def test_revocations_are_concurrent_idempotent_hashes_with_expiry_cleanup(settings):
    store = InviteSessionRevocations(settings.approvals_root / "revocations-test.sqlite3")
    tokens = [issue_cookie(settings, settings.invite_codes[0], audience="testserver")
              for _ in range(12)]
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda token: store.revoke(settings, token, audience="testserver"), tokens * 2))
    assert all(store.revoked(token) for token in tokens)
    with sqlite3.connect(store.path) as connection:
        rows = connection.execute("SELECT token_hash, expires_at FROM revoked_sessions").fetchall()
        assert {row[0] for row in rows} == {_session_token_hash(token) for token in tokens}
        connection.execute("INSERT INTO revoked_sessions VALUES (?, ?)", ("expired", 1))
    store.revoke(settings, tokens[0], audience="testserver")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM revoked_sessions").fetchone()[0] == len(tokens)

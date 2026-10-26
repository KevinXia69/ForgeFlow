"""Hosted deployment boundaries exercised through the public HTTP surface."""

from __future__ import annotations

from dataclasses import replace

import pytest
from fastapi.testclient import TestClient

from forgeflow.api import assistant
from forgeflow.api.app import create_app
from forgeflow.api.auth import issue_cookie, verify_cookie
from forgeflow.api.model_settings import ModelSettingsStore
from forgeflow.api.rate_limit import FixedWindowLimiter
from forgeflow.api.settings import create_settings
from forgeflow.product import FakeProductExecutor

SECRET = "hosted-test-secret-0123456789abcdef"
INVITE = "hosted-test-invite-000001"
SECOND_INVITE = "hosted-test-invite-000002"
HOST = "203.0.113.10"
MODEL_KEY = "hosted-test-model-key-123456"
MODEL = {"profile": "custom", "base_url": "https://provider.example/v1",
         "model": "test-model", "api_key": MODEL_KEY}


def hosted_settings(tmp_path, **changes):
    env = {
        "FORGEFLOW_DEPLOYMENT": "hosted",
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": f"{INVITE},{SECOND_INVITE}",
        "FORGEFLOW_TRUSTED_HOSTS": HOST,
        "FORGEFLOW_ENVIRONMENT_GENERATION": "a" * 32,
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        **changes,
    }
    return create_settings(env)


def login(client, invite=INVITE):
    response = client.post("/api/auth/session", json={"invite_code": invite})
    assert response.status_code == 201
    return response


@pytest.mark.parametrize("changes", [
    {"FORGEFLOW_SECURE_COOKIE": "false"},
    {"FORGEFLOW_TRUSTED_HOSTS": ""},
    {"FORGEFLOW_TRUSTED_HOSTS": "*.example.com"},
    {"FORGEFLOW_TRUSTED_HOSTS": "https://example.com"},
    {"FORGEFLOW_TRUSTED_HOSTS": "user@example.com"},
    {"FORGEFLOW_TRUSTED_HOSTS": "[bad-ip]"},
    {"FORGEFLOW_PUBLIC_RUNS": "*"},
    {"FORGEFLOW_ENVIRONMENT_GENERATION": ""},
])
def test_hosted_configuration_fails_closed(tmp_path, changes):
    with pytest.raises(ValueError):
        hosted_settings(tmp_path, **changes)


def test_hosted_hosts_support_exact_dns_and_ip_addresses(tmp_path):
    settings = hosted_settings(tmp_path, FORGEFLOW_TRUSTED_HOSTS=
                               "EXAMPLE.COM,203.0.113.10,[2001:db8::1]:8443")
    assert settings.allows_host("example.com")
    assert settings.allows_host("203.0.113.10:443")
    assert settings.allows_host("[2001:db8::1]:8443")
    assert not settings.allows_host("[2001:db8::1]:8000")
    assert not settings.allows_host("example.com.evil.test")
    assert not settings.allows_host("example.com@evil.test")


def test_revoked_invite_invalidates_existing_cookie_without_revoking_other_user(tmp_path):
    settings = hosted_settings(tmp_path)
    revoked = issue_cookie(settings, INVITE, audience=HOST)
    retained = issue_cookie(settings, SECOND_INVITE, audience=HOST)
    original_uid = verify_cookie(settings, retained, audience=HOST)
    reloaded = replace(settings, invite_codes=(SECOND_INVITE,))
    assert verify_cookie(reloaded, revoked, audience=HOST) is None
    assert verify_cookie(reloaded, retained, audience=HOST) == original_uid
    assert verify_cookie(reloaded, issue_cookie(reloaded, SECOND_INVITE, audience=HOST),
                         audience=HOST) == original_uid


def test_hosted_host_fence_applies_without_browser_headers_and_keeps_security_headers(tmp_path):
    with TestClient(create_app(hosted_settings(tmp_path)), base_url=f"https://{HOST}") as client:
        assert client.get("/api/health").status_code == 200
        for path in ("/api/health", "/api/auth/me", "/"):
            response = client.get(path, headers={"Host": "attacker.example"})
            assert response.status_code == 400
            assert response.headers["X-Content-Type-Options"] == "nosniff"
        response = client.post("/api/auth/session", json={"invite_code": INVITE},
                               headers={"Origin": "https://attacker.example"})
        assert response.status_code == 403
        assert response.headers["Cache-Control"] == "no-store"
        session = login(client)
        cookie = session.headers["set-cookie"]
        assert "secure" in cookie.lower() and "httponly" in cookie.lower()
        assert "samesite=lax" in cookie.lower()


def test_hosted_never_uses_browser_or_saved_workspace_model_settings(tmp_path, monkeypatch):
    for key, value in MODEL.items():
        suffix = "NAME" if key == "model" else key.upper()
        monkeypatch.setenv(f"FORGEFLOW_MODEL_{suffix}", value)
    settings = hosted_settings(tmp_path)
    ModelSettingsStore(settings.approvals_root).save(
        "custom", "http://127.0.0.1:9999/v1", "unsafe-workspace-model", MODEL_KEY,
    )
    calls = []
    monkeypatch.setattr(assistant, "generate", lambda _question, _passages, model:
                        calls.append(model))
    monkeypatch.setenv("FORGEFLOW_DEVELOPER_TOOLS", "1")
    monkeypatch.setenv("FORGEFLOW_DSH_WEB_URL", "http://internal/?token=private")
    with TestClient(create_app(settings), base_url=f"https://{HOST}") as client:
        login(client)
        assert client.put("/api/settings/model", json=MODEL).status_code == 403
        assert client.delete("/api/settings/model").status_code == 403
        for path, body in (
            ("/api/settings/model/test", {"model": MODEL}),
            ("/api/assistant/answer", {"question": "explain", "model": MODEL}),
            ("/api/assistant/stream", {"question": "explain", "model": MODEL}),
        ):
            response = client.post(path, json=body)
            assert response.status_code == 403
            assert response.json()["detail"] == "model_override_disabled"
        assert not calls
        model_view = client.get("/api/settings/model").json()
        assert model_view["source"] == "platform"
        assert model_view["read_only"] is True and model_view["can_override"] is False
        assert MODEL_KEY not in str(model_view)
        assert client.get("/api/config").json()["dsh_web_url"] == ""
        assert client.post("/api/settings/model/test").json()["ok"] is True
        assert calls[0].base_url == MODEL["base_url"]
        assert calls[0].model == MODEL["model"]


def test_login_limit_cannot_be_reset_by_forging_forwarding_headers(tmp_path):
    with TestClient(create_app(hosted_settings(tmp_path)), base_url=f"https://{HOST}") as client:
        for _ in range(10):
            assert client.post("/api/auth/session", json={"invite_code": "wrong"}).status_code == 401
        response = client.post("/api/auth/session", json={"invite_code": INVITE},
                               headers={"X-Forwarded-For": "198.51.100.44"})
        assert response.status_code == 429
        assert 1 <= int(response.headers["Retry-After"]) <= 60
        assert client.get("/api/health").status_code == 200


def test_model_limits_are_per_principal_and_cannot_be_reset_by_relogin(tmp_path, monkeypatch):
    for key, value in MODEL.items():
        suffix = "NAME" if key == "model" else key.upper()
        monkeypatch.setenv(f"FORGEFLOW_MODEL_{suffix}", value)
    monkeypatch.setattr(assistant, "generate", lambda *_args: "ok")
    with TestClient(create_app(hosted_settings(tmp_path)), base_url=f"https://{HOST}") as client:
        login(client)
        for _ in range(30):
            assert client.post("/api/settings/model/test").status_code == 200
        login(client)
        assert client.post("/api/settings/model/test").status_code == 429
        login(client, SECOND_INVITE)
        assert client.post("/api/settings/model/test").status_code == 200


def test_limiter_capacity_is_bounded_without_evicting_active_limits():
    now = [0.0]
    limiter = FixedWindowLimiter(max_keys=2, clock=lambda: now[0])
    assert limiter.retry_after("first", limit=1) == 0
    assert limiter.retry_after("second", limit=1) == 0
    assert limiter.retry_after("third", limit=1) == 60
    assert limiter.retry_after("first", limit=1) == 60
    now[0] = 61.0
    assert limiter.retry_after("third", limit=1) == 0
    assert limiter.retry_after("first", limit=1) == 0


def test_hosted_environment_lease_is_enforced_at_the_http_boundary(tmp_path):
    executor = FakeProductExecutor()
    app = create_app(hosted_settings(tmp_path), product_executor=executor)
    with TestClient(app, base_url=f"https://{HOST}") as first, \
            TestClient(app, base_url=f"https://{HOST}") as second:
        assert first.get("/api/environment").status_code == 401
        login(first)
        login(second, SECOND_INVITE)
        assert first.get("/api/environment").json()["available"] is True
        created = first.post("/api/product/cases", json={"scenario": "A"})
        assert created.status_code == 201
        blocked = second.post("/api/product/cases", json={"scenario": "A"})
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == "environment_busy"
        assert second.get("/api/environment").json()["requires_admin_reset"] is True
        caps = second.get("/api/capabilities").json()
        assert caps["case_create"] is False and caps["reason"] == "environment_busy"
        case_id = created.json()["case_id"]
        assert second.post(f"/api/product/cases/{case_id}/prepare").status_code == 404
        assert second.get(f"/api/product/cases/{case_id}").status_code == 404
        assert executor.prepare_calls == 1

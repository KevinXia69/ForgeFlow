from __future__ import annotations

import re

from fastapi.testclient import TestClient

from forgeflow.api import web
from forgeflow.api.app import create_app
from forgeflow.api.settings import Settings
from forgeflow.api.web import mount_web

SECRET = "test-session-secret-0123456789abcdef"
INVITE = "invite-code-for-tests-0001"


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    mount_web(app, settings)
    return TestClient(app, base_url="https://testserver")


def test_root_serves_a_console(settings: Settings) -> None:
    with _client(settings) as client:
        response = client.get("/")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    html = response.text
    if 'id="root"' in html:
        # The React workbench is built; assets are served below /app/.
        assert "/app/assets/" in html
    else:
        assert 'id="login-form"' in html
        assert "/static/app.js" in html
    assert 'name="referrer" content="no-referrer"' in html


def test_static_assets_are_served(settings: Settings) -> None:
    with _client(settings) as client:
        for path, marker in (
            ("/static/app.js", 'credentials: "same-origin"'),
            ("/static/style.css", ":focus-visible"),
        ):
            response = client.get(path)
            assert response.status_code == 200
            assert response.text.strip()
            assert marker in response.text


def test_mount_web_does_not_shadow_the_api(settings: Settings) -> None:
    app = create_app(settings)
    mount_web(app, settings)
    with TestClient(app, base_url="https://testserver") as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json() == {"ok": True}
        session = client.post("/api/auth/session", json={"invite_code": INVITE})
        assert session.status_code == 201


def test_html_references_no_external_scripts_or_styles(settings: Settings) -> None:
    with _client(settings) as client:
        html = client.get("/").text
    urls = re.findall(r'(?:src|href)="([^"]+)"', html)
    assert urls
    assert not any(url.lower().startswith(("http://", "https://", "//")) for url in urls)


def test_javascript_uses_the_approval_endpoints_and_same_origin_credentials(
    settings: Settings,
) -> None:
    with _client(settings) as client:
        script = client.get("/static/app.js").text
    assert 'credentials: "same-origin"' in script
    assert "/approvals" in script
    assert "ttl_seconds" in script
    assert '"POST"' in script or "method: \"POST\"" in script


def test_missing_static_directory_leaves_the_api_working(
    settings: Settings, monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(web, "STATIC_DIR", tmp_path / "absent-static")
    app = create_app(settings)
    mount_web(app, settings)
    with TestClient(app, base_url="https://testserver") as client:
        assert client.get("/api/health").json() == {"ok": True}
        assert client.post(
            "/api/auth/session", json={"invite_code": INVITE}
        ).status_code == 201


def test_the_console_exposes_the_knowledge_panel():
    from forgeflow.api.web import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text()
    script = (STATIC_DIR / "app.js").read_text()
    assert 'id="kb-form"' in html and 'id="kb-results"' in html
    assert "/api/knowledge/answer" in script
    assert "textContent" in script and "innerHTML" not in script


def test_the_knowledge_panel_keeps_a_conversation():
    from forgeflow.api.web import STATIC_DIR

    script = (STATIC_DIR / "app.js").read_text()
    assert "const conversation" in script
    assert "appendTurn" in script
    assert "kb-turn" in script


def test_the_console_subscribes_to_the_event_stream():
    from forgeflow.api.web import STATIC_DIR

    script = (STATIC_DIR / "app.js").read_text()
    assert "EventSource" in script
    assert "/events" in script
    assert "watchRun" in script


def test_the_console_can_create_a_run():
    from forgeflow.api.web import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text()
    script = (STATIC_DIR / "app.js").read_text()
    assert 'id="create-run"' in html and 'id="scenario-select"' in html
    assert '"/api/runs"' in script and "idempotency_key" in script


def test_the_console_restores_a_session_and_selected_run():
    from forgeflow.api.web import STATIC_DIR

    script = (STATIC_DIR / "app.js").read_text()
    assert "restoreSession" in script
    assert "location.hash" in script
    assert "#run=" in script


def test_review_mode_hides_writes(tmp_path):
    from forgeflow.api.settings import create_settings

    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        "FORGEFLOW_SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "FORGEFLOW_INVITE_CODES": "invite-code-for-tests-0001",
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_DEPLOYMENT": "review",
    })
    (tmp_path / "runs").mkdir()
    with _client(settings) as client:
        config = client.get("/api/config").json()
        assert config["deployment"] == "review"
        assert config["runner_enabled"] is False
    from forgeflow.api.web import STATIC_DIR

    html = (STATIC_DIR / "index.html").read_text()
    script = (STATIC_DIR / "app.js").read_text()
    assert 'id="review-banner"' in html
    assert "/api/config" in script and "review-banner" in script


def test_sessions_endpoint_requires_auth_and_returns_projection(settings: Settings, monkeypatch) -> None:
    with _client(settings) as client:
        assert client.get("/api/sessions").status_code == 401
        assert client.post("/api/auth/session", json={"invite_code": INVITE}).status_code == 201
        response = client.get("/api/sessions")
    assert response.status_code == 200
    assert response.json() == []


def test_browser_trust_fence_rejects_cross_site_and_untrusted_hosts(settings: Settings) -> None:
    with _client(settings) as client:
        assert client.get("/api/health").status_code == 200
        same_origin = client.get(
            "/api/health",
            headers={
                "host": "127.0.0.1:8000",
                "origin": "http://127.0.0.1:8000",
                "sec-fetch-site": "same-origin",
            },
        )
        assert same_origin.status_code == 200
        cross_site = client.get(
            "/api/health",
            headers={
                "host": "127.0.0.1:8000",
                "origin": "https://evil.example",
                "sec-fetch-site": "cross-site",
            },
        )
        assert cross_site.status_code == 403
        rebound = client.get(
            "/api/health",
            headers={
                "host": "evil.example",
                "origin": "https://evil.example",
                "sec-fetch-site": "same-origin",
            },
        )
        assert rebound.status_code == 403


def test_native_shell_url_is_owner_only(settings: Settings, monkeypatch) -> None:
    monkeypatch.setenv("FORGEFLOW_DEVELOPER_TOOLS", "1")
    token_url = "http://127.0.0.1:59596/?token=test-owner-token"
    monkeypatch.setenv("FORGEFLOW_DSH_WEB_URL", token_url)
    with _client(settings) as client:
        anonymous = client.get("/api/config")
        assert anonymous.status_code == 200
        assert anonymous.json()["deployment"] == "local"
        assert anonymous.json()["dsh_web_url"] == ""
        assert token_url not in anonymous.text

        assert client.post(
            "/api/auth/session", json={"invite_code": INVITE}
        ).status_code == 201
        owner = client.get("/api/config")
        assert owner.status_code == 200
        assert owner.json()["dsh_web_url"] == token_url


def test_review_never_exposes_the_native_shell_url(tmp_path, monkeypatch) -> None:
    from forgeflow.api.settings import create_settings

    monkeypatch.setenv("FORGEFLOW_DSH_WEB_URL", "http://127.0.0.1:59596/?token=test-owner-token")
    review = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
        "FORGEFLOW_DEPLOYMENT": "review",
    })
    (tmp_path / "runs").mkdir()
    with _client(review) as client:
        assert client.post(
            "/api/auth/session", json={"invite_code": INVITE}
        ).status_code == 201
        config = client.get("/api/config").json()
    assert config["deployment"] == "review"
    assert config["dsh_web_url"] == ""


def test_the_built_workbench_is_served(settings: Settings) -> None:
    from forgeflow.api.web import APP_DIR

    if not (APP_DIR / "index.html").is_file():
        import pytest

        pytest.skip("React build not present")
    assets = sorted((APP_DIR / "assets").glob("*.js"))
    assert assets, "Vite build has no JS asset"
    with _client(settings) as client:
        root = client.get("/")
        assert 'id="root"' in root.text
        assert "/app/assets/" in root.text
        asset = client.get(f"/app/assets/{assets[0].name}")
        assert asset.status_code == 200
        assert asset.text

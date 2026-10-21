"""Independent regression tests for the DSH native-shell security boundary.

These tests deliberately re-derive the expected behaviour from the public
contract (authenticated owner, loopback/trusted authority, same-origin) rather
than mirroring the implementation.  They complement ``test_api_web.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request

from forgeflow.api.app import create_app
from forgeflow.api.settings import Settings, create_settings
from forgeflow.api.trust import is_trusted_api_request

SECRET = "independent-test-secret-0123456789abcdef"
INVITE = "independent-invite-code-0001"
TOKEN = "owner-token-do-not-expose-9f8e7d6c5b4a"
TOKEN_URL = f"http://127.0.0.1:59596/?token={TOKEN}"


def _settings(tmp_path: Path, *, deployment: str = "local") -> Settings:
    (tmp_path / "runs").mkdir(exist_ok=True)
    return create_settings(
        {
            "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
            "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
            "FORGEFLOW_SESSION_SECRET": SECRET,
            "FORGEFLOW_INVITE_CODES": INVITE,
            "FORGEFLOW_SECURE_COOKIE": "false",
            "FORGEFLOW_DEPLOYMENT": deployment,
        }
    )


def _client(
    settings: Settings,
    monkeypatch: pytest.MonkeyPatch,
    *,
    token_url: str | None = TOKEN_URL,
    url_file: Path | None = None,
) -> TestClient:
    if token_url is None:
        monkeypatch.delenv("FORGEFLOW_DSH_WEB_URL", raising=False)
    else:
        monkeypatch.setenv("FORGEFLOW_DSH_WEB_URL", token_url)
    if url_file is None:
        monkeypatch.delenv("FORGEFLOW_DSH_WEB_URL_FILE", raising=False)
    else:
        monkeypatch.setenv("FORGEFLOW_DSH_WEB_URL_FILE", str(url_file))
    return TestClient(create_app(settings), base_url="http://127.0.0.1:8000")


def _login(client: TestClient) -> None:
    response = client.post("/api/auth/session", json={"invite_code": INVITE})
    assert response.status_code == 201


# ---------------------------------------------------------------------------
# /api/config: the owner-token URL is an authenticated local-only credential.
# ---------------------------------------------------------------------------


def test_anonymous_config_never_contains_native_shell_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(_settings(tmp_path), monkeypatch) as client:
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["deployment"] == "local"
    assert body["runner_enabled"] is False
    assert body["dsh_web_url"] == ""
    assert TOKEN not in response.text
    assert "token=" not in response.text


def test_forged_or_malformed_session_cookie_does_not_unlock_native_shell_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _settings(tmp_path)
    with _client(settings, monkeypatch) as client:
        for cookie in ("forged", "forged.signature", "not-a-cookie"):
            client.headers["cookie"] = f"{settings.cookie_name}={cookie}"
            response = client.get("/api/config")
            assert response.status_code == 200
            assert response.json()["dsh_web_url"] == ""
            assert TOKEN not in response.text
        del client.headers["cookie"]


def test_authenticated_local_owner_receives_native_shell_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEFLOW_DEVELOPER_TOOLS", "1")
    with _client(_settings(tmp_path), monkeypatch) as client:
        _login(client)
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["deployment"] == "local"
    assert body["runner_enabled"] is False
    assert body["dsh_web_url"] == TOKEN_URL


def test_authenticated_local_owner_can_use_private_url_file_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FORGEFLOW_DEVELOPER_TOOLS", "1")
    url_file = tmp_path / "dsh-web-url"
    url_file.write_text(TOKEN_URL + "\n", encoding="utf-8")
    with _client(_settings(tmp_path), monkeypatch, token_url=None, url_file=url_file) as client:
        assert client.get("/api/config").json()["dsh_web_url"] == ""
        _login(client)
        response = client.get("/api/config")

    assert response.status_code == 200
    assert response.json()["dsh_web_url"] == TOKEN_URL


def test_review_mode_hides_native_shell_url_even_from_authenticated_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _client(_settings(tmp_path, deployment="review"), monkeypatch) as client:
        _login(client)
        response = client.get("/api/config")

    assert response.status_code == 200
    body = response.json()
    assert body["deployment"] == "review"
    assert body["dsh_web_url"] == ""
    assert TOKEN not in response.text


# ---------------------------------------------------------------------------
# is_trusted_api_request: pure Host/Origin/Sec-Fetch-Site fence.
# ---------------------------------------------------------------------------


def _request(
    *,
    host: str | None = None,
    origin: str | None = None,
    site: str | None = None,
) -> Request:
    headers: list[tuple[bytes, bytes]] = []
    if host is not None:
        headers.append((b"host", host.encode()))
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if site is not None:
        headers.append((b"sec-fetch-site", site.encode()))
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/health",
        "raw_path": b"/api/health",
        "query_string": b"",
        "headers": headers,
        "client": ("127.0.0.1", 54321),
        "server": ("127.0.0.1", 8000),
    }
    return Request(scope)


def _trust_settings(**extra: str) -> Settings:
    env = {
        "FORGEFLOW_SESSION_SECRET": SECRET,
        "FORGEFLOW_INVITE_CODES": INVITE,
    }
    env.update(extra)
    return create_settings(env)


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("127.0.0.1:8000", "http://127.0.0.1:8000"),
        ("127.0.0.1", "http://127.0.0.1"),
        ("localhost:5173", "http://localhost:5173"),
    ],
)
def test_loopback_same_origin_is_accepted(host: str, origin: str) -> None:
    request = _request(host=host, origin=origin, site="same-origin")
    assert is_trusted_api_request(request, _trust_settings())


def test_same_origin_fetch_without_origin_header_is_accepted() -> None:
    request = _request(host="127.0.0.1:8000", site="same-origin")
    assert is_trusted_api_request(request, _trust_settings())


def test_cross_site_request_is_rejected() -> None:
    request = _request(
        host="127.0.0.1:8000",
        origin="http://127.0.0.1:8000",
        site="cross-site",
    )
    assert not is_trusted_api_request(request, _trust_settings())


def test_cross_origin_request_is_rejected() -> None:
    request = _request(
        host="127.0.0.1:8000",
        origin="https://evil.example",
        site="same-site",
    )
    assert not is_trusted_api_request(request, _trust_settings())


def test_untrusted_host_is_rejected() -> None:
    request = _request(
        host="evil.example",
        origin="https://evil.example",
        site="same-origin",
    )
    assert not is_trusted_api_request(request, _trust_settings())


def test_untrusted_host_without_origin_header_is_rejected() -> None:
    request = _request(host="evil.example", site="same-origin")
    assert not is_trusted_api_request(request, _trust_settings())


def test_null_origin_is_rejected() -> None:
    request = _request(
        host="127.0.0.1:8000",
        origin="null",
        site="same-origin",
    )
    assert not is_trusted_api_request(request, _trust_settings())


def test_port_mismatch_is_rejected() -> None:
    request = _request(
        host="127.0.0.1:8000",
        origin="http://127.0.0.1:8001",
        site="same-origin",
    )
    assert not is_trusted_api_request(request, _trust_settings())


def test_non_http_origin_is_rejected() -> None:
    request = _request(
        host="127.0.0.1:8000",
        origin="ftp://127.0.0.1:8000",
        site="same-origin",
    )
    assert not is_trusted_api_request(request, _trust_settings())


@pytest.mark.parametrize(
    ("host", "origin"),
    [
        ("[::1]:8000", "http://[::1]:8000"),
        ("[::1]", "http://[::1]"),
    ],
)
def test_ipv6_loopback_same_origin_is_accepted(host: str, origin: str) -> None:
    request = _request(host=host, origin=origin, site="same-origin")
    assert is_trusted_api_request(request, _trust_settings())


def test_render_external_hostname_is_trusted_via_create_settings() -> None:
    settings = _trust_settings(RENDER_EXTERNAL_HOSTNAME="forgeflow-review.onrender.com")
    assert "forgeflow-review.onrender.com" in settings.trusted_hosts
    request = _request(
        host="forgeflow-review.onrender.com",
        origin="https://forgeflow-review.onrender.com",
        site="same-origin",
    )
    assert is_trusted_api_request(request, settings)


def test_render_hostname_is_untrusted_without_configuration() -> None:
    request = _request(
        host="forgeflow-review.onrender.com",
        origin="https://forgeflow-review.onrender.com",
        site="same-origin",
    )
    assert not is_trusted_api_request(request, _trust_settings())


def test_explicitly_configured_trusted_host_is_accepted() -> None:
    settings = _trust_settings(FORGEFLOW_TRUSTED_HOSTS="workbench.internal:8443")
    request = _request(
        host="workbench.internal:8443",
        origin="https://workbench.internal:8443",
        site="same-origin",
    )
    assert is_trusted_api_request(request, settings)

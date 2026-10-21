from __future__ import annotations

import re
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.auth import issue_cookie, verify_cookie
from forgeflow.api.settings import Settings, create_settings
from forgeflow.runview import build_run_view


def _auth(client: TestClient, settings: Settings) -> dict[str, str]:
    response = client.post(
        "/api/auth/session", json={"invite_code": settings.invite_codes[0]}
    )
    assert response.status_code == 201
    return {"cookie": response.headers["set-cookie"].split(";", 1)[0]}


def _order(root: Path) -> list[str]:
    views = [
        build_run_view(path, root)
        for path in root.iterdir()
        if path.is_dir() and not path.is_symlink()
    ]
    rows = [view for view in views if view is not None]
    ordered = sorted(rows, key=lambda row: row["timing"]["started_at"], reverse=True)
    return [row["run_id"] for row in ordered]


def test_health_is_public_and_every_response_has_security_headers(client, settings):
    health = client.get("/api/health")
    assert health.status_code == 200
    assert health.json() == {"ok": True}
    anonymous = client.get("/api/runs")
    assert anonymous.status_code == 401
    assert anonymous.json() == {"detail": "authentication required"}
    missing = client.get("/api/nope")
    assert missing.status_code == 404
    for response in (health, anonymous, missing):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["cache-control"] == "no-store"


def test_session_issues_a_hardened_cookie_and_rejects_bad_codes(client, settings):
    response = client.post(
        "/api/auth/session", json={"invite_code": settings.invite_codes[0]}
    )
    assert response.status_code == 201
    body = response.json()
    assert re.fullmatch(r"[0-9a-f]{26}", body["uid"])
    assert body["expires_at"] > int(time.time())
    header = response.headers["set-cookie"].lower()
    for attribute in ("httponly", "secure", "samesite=lax", "path=/"):
        assert attribute in header
    assert verify_cookie(settings, response.cookies[settings.cookie_name], audience="testserver") == body["uid"]
    bad = client.post("/api/auth/session", json={"invite_code": "not-a-configured-code"})
    assert bad.status_code == 401
    assert "set-cookie" not in bad.headers


def test_runs_list_requires_a_cookie_and_returns_sorted_summaries(client, settings):
    (settings.data_root / "broken-run").mkdir()
    assert client.get("/api/runs").status_code == 401
    headers = _auth(client, settings)
    response = client.get("/api/runs", headers=headers)
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 2
    assert set(rows[0]) == {
        "run_id",
        "task",
        "task_label",
        "score",
        "strict_success",
        "evaluation_valid",
        "security_profile",
        "access_scope",
        "started_at",
        "state",
        "agent",
        "experiment",
    }
    assert [row["run_id"] for row in rows] == _order(settings.data_root)
    view = build_run_view(settings.data_root / rows[0]["run_id"], settings.data_root)
    assert rows[0] == {
        "run_id": view["run_id"],
        "task": view["task"]["id"],
        "task_label": view["task"]["label"],
        "score": view["evaluation"]["score"],
        "strict_success": view["evaluation"]["strict_success"],
        "evaluation_valid": view["evaluation"]["evaluation_valid"],
        "security_profile": view["experiment"]["security_profile"]["name"],
        "started_at": view["timing"]["started_at"],
        "state": view["state"],
        "agent": view["agent"],
        "experiment": view["experiment"],
        "access_scope": "public_evidence",
    }


def test_run_detail_serves_projection_and_rejects_bad_ids(client, settings):
    run_id = _order(settings.data_root)[0]
    assert client.get(f"/api/runs/{run_id}").status_code == 401
    headers = _auth(client, settings)
    response = client.get(f"/api/runs/{run_id}", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["schema_version"] == 1
    assert body["kind"] == "forgeflow.run_view"
    assert body["run_id"] == run_id
    assert client.get("/api/runs/does-not-exist", headers=headers).status_code == 404
    assert client.get("/api/runs/..%2F.env", headers=headers).status_code == 400


def test_foreign_expired_and_tampered_cookies_are_rejected(client, settings):
    code = settings.invite_codes[0]
    foreign = create_settings(
        {
            "FORGEFLOW_SESSION_SECRET": "a-different-session-secret-000000",
            "FORGEFLOW_INVITE_CODES": code,
        }
    )
    expired_at = int(time.time()) - settings.session_ttl_sec - 10
    tampered = "x" + issue_cookie(settings, code)[1:]
    for value in (
        issue_cookie(foreign, code),
        issue_cookie(settings, code, now=expired_at),
        tampered,
    ):
        response = client.get(
            "/api/runs", headers={"cookie": f"{settings.cookie_name}={value}"}
        )
        assert response.status_code == 401


def test_create_settings_defaults_and_validation_never_echo_secrets():
    secret = "s" * 40
    code = "c" * 20
    default = create_settings(
        {"FORGEFLOW_SESSION_SECRET": secret, "FORGEFLOW_INVITE_CODES": code}
    )
    assert default.data_root == Path("runs")
    assert default.session_ttl_sec == 12 * 60 * 60
    assert default.cookie_name == "ff_session"
    assert default.secure_cookie is True
    custom = create_settings(
        {
            "FORGEFLOW_SESSION_SECRET": secret,
            "FORGEFLOW_INVITE_CODES": f"{code},{'d' * 16}",
            "FORGEFLOW_DATA_ROOT": "/tmp/example",
            "FORGEFLOW_SESSION_TTL_SEC": "60",
            "FORGEFLOW_SECURE_COOKIE": "false",
        }
    )
    assert custom.invite_codes == (code, "d" * 16)
    assert custom.data_root == Path("/tmp/example")
    assert custom.session_ttl_sec == 60
    assert custom.secure_cookie is False
    invalid = (
        {},
        {"FORGEFLOW_INVITE_CODES": code},
        {"FORGEFLOW_SESSION_SECRET": "short", "FORGEFLOW_INVITE_CODES": code},
        {"FORGEFLOW_SESSION_SECRET": secret, "FORGEFLOW_INVITE_CODES": "short"},
        {
            "FORGEFLOW_SESSION_SECRET": secret,
            "FORGEFLOW_INVITE_CODES": code,
            "FORGEFLOW_SESSION_TTL_SEC": "0",
        },
    )
    for env in invalid:
        with pytest.raises(ValueError) as error:
            create_settings(env)
        assert secret not in str(error.value)


def test_ownerless_runs_require_an_explicit_public_allowlist(runs_root, settings):
    from forgeflow.api.app import create_app
    from forgeflow.api.settings import create_settings

    private = create_settings({
        "FORGEFLOW_DATA_ROOT": str(runs_root),
        "FORGEFLOW_SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "FORGEFLOW_INVITE_CODES": "invite-code-for-tests-0001",
        "FORGEFLOW_SECURE_COOKIE": "true",
    })
    with TestClient(create_app(private), base_url="https://testserver") as client:
        headers = _auth(client, private)
        assert client.get("/api/runs", headers=headers).json() == []
        first = next(path for path in runs_root.iterdir() if path.is_dir())
        assert client.get(f"/api/runs/{first.name}", headers=headers).status_code == 404

    public = create_settings({
        "FORGEFLOW_DATA_ROOT": str(runs_root),
        "FORGEFLOW_SESSION_SECRET": "test-session-secret-0123456789abcdef",
        "FORGEFLOW_INVITE_CODES": "invite-code-for-tests-0001",
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_PUBLIC_RUNS": "*",
    })
    with TestClient(create_app(public), base_url="https://testserver") as client:
        headers = _auth(client, public)
        rows = client.get("/api/runs", headers=headers).json()
        assert len(rows) == 2
        assert client.get(f"/api/runs/{rows[0]['run_id']}", headers=headers).status_code == 200


def test_capabilities_separate_review_and_live_execution(client, settings):
    headers = _auth(client, settings)
    body = client.get("/api/capabilities", headers=headers).json()
    assert body["project_qa"] is True
    assert body["historical_review"] is True
    assert body["live_prepare"] is False
    assert body["live_execute"] is False
    assert body["product_approval"] is False
    assert body["deployment"] == "local"


def test_knowledge_endpoint_selects_the_requested_language_corpus(client, settings):
    headers = _auth(client, settings)
    question = "What is ForgeFlow's product positioning for manufacturing exception handling?"
    english = client.post(
        "/api/knowledge/answer",
        json={"question": question, "lang": "en"},
        headers=headers,
    )
    assert english.status_code == 200
    assert english.json()["found"] is True
    assert all(c.startswith("docs/en/") for c in english.json()["citations"])

    chinese = client.post(
        "/api/knowledge/answer",
        json={"question": question, "lang": "zh"},
        headers=headers,
    )
    assert chinese.status_code == 200
    assert not any(c.startswith("docs/en/") for c in chinese.json()["citations"])

"""Browser policies preserve features while violation collection drops content."""
import json
import logging

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.browser_security import MAX_REPORT_BYTES, POLICY_HEADERS, REPORT_PATH
from forgeflow.api.settings import create_settings


@pytest.fixture
def browser_app(tmp_path):
    settings = create_settings({
        "FORGEFLOW_DEPLOYMENT": "hosted",
        "FORGEFLOW_SESSION_SECRET": "synthetic-browser-secret-1234567890",
        "FORGEFLOW_INVITE_CODES": "synthetic-browser-invite",
        "FORGEFLOW_TRUSTED_HOSTS": "forgeflow.example",
        "FORGEFLOW_ENVIRONMENT_GENERATION": "b" * 32,
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
    })
    return create_app(settings)


def test_browser_headers_cover_login_health_and_error_without_enforcing_tt(browser_app):
    with TestClient(browser_app, base_url="https://forgeflow.example") as client:
        for path in ("/app/", "/api/health", "/api/auth/me", "/missing"):
            response = client.get(path)
            for name, value in POLICY_HEADERS.items():
                assert response.headers[name] == value
            assert response.headers["Reporting-Endpoints"] == (
                'forgeflow-csp="https://forgeflow.example/api/browser-reports"')
            assert "require-trusted-types-for" not in response.headers["Content-Security-Policy"]
            assert "require-trusted-types-for 'script'" in response.headers["Content-Security-Policy-Report-Only"]
        denied = client.get("/api/health", headers={"Host": 'evil.example'})
        assert denied.status_code == 400
        assert "Reporting-Endpoints" not in denied.headers


def test_report_formats_drop_urls_samples_and_identity(browser_app, caplog):
    secret = "SYNTHETIC-NEVER-LOG-THIS"
    body = {"effective-directive": "img-src", "disposition": "report",
            "blocked-uri": f"https://{secret}.example/{secret}?token={secret}",
            "document-uri": f"https://forgeflow.example/app/?token={secret}",
            "source-file": secret, "script-sample": secret, "line-number": 9,
            "original-policy": secret, "user": secret}
    with caplog.at_level(logging.INFO, logger="forgeflow.api.browser_security"):
        with TestClient(browser_app, base_url="https://forgeflow.example") as client:
            response = client.post(REPORT_PATH, json={"csp-report": body},
                                   headers={"Content-Type": "application/csp-report", "Origin": "https://forgeflow.example"})
            assert response.status_code == 204
            response = client.post(REPORT_PATH, json=[{"type": "csp-violation", "url": secret,
                "body": {"effectiveDirective": "require-trusted-types-for", "disposition": "report",
                         "blockedURL": "trusted-types-sink", "sample": secret}}],
                headers={"Content-Type": "application/reports+json"})
            assert response.status_code == 204
    counters = browser_app.state.browser_reports.snapshot()
    assert counters == [
        {"directive": "img-src", "disposition": "report", "resource": "https", "count": 1},
        {"directive": "require-trusted-types-for", "disposition": "report", "resource": "trusted-types-sink", "count": 1},
    ]
    assert secret not in json.dumps(counters)
    assert secret not in caplog.text


@pytest.mark.parametrize("body", [None, [], "raw", {"effective-directive": []},
    {"effective-directive": {}}, {"effective-directive": "script-src", "disposition": []},
    {"effective-directive": "img-src", "blocked-uri": "https://[bad"},
    {"effective-directive": "invented-unbounded-label"}])
def test_malformed_report_values_never_break_the_receiver(browser_app, body):
    with TestClient(browser_app, base_url="https://forgeflow.example") as client:
        assert client.post(REPORT_PATH, json={"csp-report": body}).status_code == 204


def test_report_size_media_batch_trust_and_admission_are_bounded(browser_app):
    with TestClient(browser_app, base_url="https://forgeflow.example") as client:
        assert client.post(REPORT_PATH, content=b"x" * (MAX_REPORT_BYTES + 1),
                           headers={"Content-Type": "application/csp-report"}).status_code == 413
        assert client.post(REPORT_PATH, content=b"{}", headers={"Content-Type": "text/plain"}).status_code == 415
        assert client.post(REPORT_PATH, content=b"{", headers={"Content-Type": "application/json"}).status_code == 400
        assert client.post(REPORT_PATH, json=[{}] * 21).status_code == 400
        assert client.post(REPORT_PATH, json={"csp-report": {}},
                           headers={"Origin": "https://evil.example"}).status_code == 403
        codes = [client.post(REPORT_PATH, json={"csp-report": {}}).status_code for _ in range(35)]
        assert 204 in codes and codes[-1] == 429
        assert browser_app.state.browser_reports.snapshot() == []

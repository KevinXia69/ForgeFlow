"""Gateway QA admission is tested with HTTP transport stubs, never a VPS."""
import importlib.util
import json
from pathlib import Path

import httpx
import pytest

pytest.importorskip("pwd")

SPEC = importlib.util.spec_from_file_location(
    "vps_gateway_acceptance_test", Path(__file__).resolve().parents[1] / "scripts/vps_gateway_acceptance.py")
acceptance = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(acceptance)


@pytest.mark.parametrize("names", [["qa4"], ["qa3", "qa3"], ["../qa3"], []])
def test_invalid_reviewer_selection_fails_before_private_reads_or_http(monkeypatch, names):
    monkeypatch.setattr(acceptance, "trusted_json", lambda _: pytest.fail("Unexpected private read"))
    with pytest.raises(acceptance.AcceptanceFailure, match="invalid_reviewer_names"):
        acceptance.run("https://example.test", names)


@pytest.mark.parametrize(("base", "trusted_hosts", "expected_host"), [
    ("https://forgeflowagent.com", ["168.144.242.231", "forgeflowagent.com"], "forgeflowagent.com"),
    ("https://168.144.242.231", ["forgeflowagent.com", "168.144.242.231"], "168.144.242.231"),
    ("https://example.test:8443", ["168.144.242.231", "example.test:8443"], "example.test:8443"),
    ("https://EXAMPLE.test:443/", ["example.test:443"], "example.test:443"),
    ("http://127.0.0.1:8090", ["forgeflowagent.com", "168.144.242.231"], "forgeflowagent.com"),
])
def test_gateway_checks_use_actual_https_authority_or_loopback_forwarded_host(
    monkeypatch, base, trusted_hosts, expected_host,
):
    private = "private-qa3-credential-do-not-export"
    generation = "3" * 32
    calls = []
    index = {"trusted_hosts": trusted_hosts, "instances": {
        "qa1": {"invite_code": "untouched-qa1"}, "qa2": {"invite_code": "untouched-qa2"},
        "qa3": {"invite_code": private, "config": "/etc/forgeflow/instances/qa3.json"}}}
    files = {"/etc/forgeflow/reviewers.json": index,
             "/etc/forgeflow/instances/qa3.json": {"sandbox_root": "/var/lib/forgeflow/reviewers/qa3/sandbox"},
             "/var/lib/forgeflow/reviewers/qa3/sandbox/vps-instance.json": {"generation": generation}}
    monkeypatch.setattr(acceptance, "trusted_json", lambda path: files[str(path)])

    def response(request):
        path, method = request.url.path, request.method
        calls.append((method, path, request.content))
        assert request.headers["host"] == expected_host
        assert request.headers["origin"] in {"https://" + expected_host, "https://untrusted.example"}
        assert (request.url.scheme, request.url.host, request.url.port) == (
            httpx.URL(base).scheme, httpx.URL(base).host, httpx.URL(base).port)
        if path == "/api/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/app/":
            return httpx.Response(200, text="<html>reviewer workbench</html>")
        if path == "/api/auth/session":
            if method == "DELETE":
                return httpx.Response(204, headers=[("set-cookie", "ff_session=; Secure; HttpOnly; SameSite=Lax"),
                    ("set-cookie", "__Host-ff_route=; Secure; HttpOnly; SameSite=Lax")])
            assert method == "POST"
            if json.loads(request.content)["invite_code"] != private:
                return httpx.Response(401)
            return httpx.Response(201, headers=[
                ("set-cookie", "ff_session=session-qa3-private; Path=/; Secure; HttpOnly; SameSite=Lax"),
                ("set-cookie", "__Host-ff_route=route-qa3-private; Path=/; Secure; HttpOnly; SameSite=Lax")])
        assert method == "GET", "No business mutation is allowed in gateway acceptance"
        if request.headers["origin"] != "https://" + expected_host:
            return httpx.Response(403)
        if request.headers.get("cookie") != "ff_session=session-qa3-private; __Host-ff_route=route-qa3-private":
            return httpx.Response(401)
        if path == "/api/auth/me":
            return httpx.Response(200, json={"scope_id": "qa3-scope"})
        if path == "/api/environment":
            return httpx.Response(200, json={"generation": generation, "available": True, "owned_by_me": False})
        if path == "/api/capabilities":
            return httpx.Response(200, json={"environment": {"execution": "live_sandbox"}})
        assert path == "/api/product/cases"
        return httpx.Response(200, json=[])

    client = httpx.Client
    monkeypatch.setattr(acceptance.httpx, "Client", lambda **kwargs: client(
        **kwargs, transport=httpx.MockTransport(response)))
    result = acceptance.run(base, ["qa3"], fresh=True)
    assert result["tested_host"] == expected_host
    assert result["tested_origin"] == "https://" + expected_host
    assert result["mode"] == "fresh_reviewers" and result["business_writes"] is False
    assert result["instances"][0]["instance"] == "qa3"
    assert result["instances"][0]["generation"] == generation
    assert result["cross_workspace_cookie_pair_rejected"] is False  # One tenant does not prove isolation.
    assert "private" not in json.dumps(result)
    assert not any(b"untouched-qa" in body for _, _, body in calls)
    assert all(method == "GET" or path == "/api/auth/session" for method, path, _ in calls)


@pytest.mark.parametrize(("base", "label"), [
    ("https://forgeflowagent.com:8443", "trusted_https_or_loopback_required"),
    ("https://forgeflowagent.com:443", "trusted_https_or_loopback_required"),
    ("https://example.test", "trusted_https_or_loopback_required"),
    ("https://example.test:9443", "trusted_https_or_loopback_required"),
    ("https://untrusted.test", "trusted_https_or_loopback_required"),
    ("http://forgeflowagent.com", "trusted_https_or_loopback_required"),
    ("http://localhost:8090", "trusted_https_or_loopback_required"),
    ("https://user@forgeflowagent.com", "invalid_acceptance_base_url"),
    ("https://forgeflowagent.com:65536", "invalid_acceptance_base_url"),
    ("https://forgeflowagent.com:invalid", "invalid_acceptance_base_url"),
    ("https://forgeflowagent.com\n", "invalid_acceptance_base_url"),
])
def test_untrusted_authorities_and_malformed_ports_fail_before_http(monkeypatch, base, label):
    monkeypatch.setattr(acceptance, "trusted_json", lambda _: {
        "trusted_hosts": ["forgeflowagent.com", "example.test:8443"]})
    monkeypatch.setattr(acceptance.httpx, "Client", lambda **_: pytest.fail("Unexpected HTTP client"))
    with pytest.raises(acceptance.AcceptanceFailure, match=label):
        acceptance.run(base, ["qa3"])

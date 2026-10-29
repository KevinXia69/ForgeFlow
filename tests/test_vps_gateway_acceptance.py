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


def test_qa3_fresh_gateway_checks_use_only_its_invite_and_never_write_business_data(monkeypatch):
    private = "private-qa3-credential-do-not-export"
    generation = "3" * 32
    calls = []
    index = {"trusted_hosts": ["example.test"], "instances": {
        "qa1": {"invite_code": "untouched-qa1"}, "qa2": {"invite_code": "untouched-qa2"},
        "qa3": {"invite_code": private, "config": "/etc/forgeflow/instances/qa3.json"}}}
    files = {"/etc/forgeflow/reviewers.json": index,
             "/etc/forgeflow/instances/qa3.json": {"sandbox_root": "/var/lib/forgeflow/reviewers/qa3/sandbox"},
             "/var/lib/forgeflow/reviewers/qa3/sandbox/vps-instance.json": {"generation": generation}}
    monkeypatch.setattr(acceptance, "trusted_json", lambda path: files[str(path)])

    def response(request):
        path, method = request.url.path, request.method
        calls.append((method, path, request.content))
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
        if request.headers["origin"] != "https://example.test":
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
    result = acceptance.run("https://example.test", ["qa3"], fresh=True)
    assert result["mode"] == "fresh_reviewers" and result["business_writes"] is False
    assert result["instances"][0]["instance"] == "qa3"
    assert result["instances"][0]["generation"] == generation
    assert result["cross_workspace_cookie_pair_rejected"] is False  # One tenant does not prove isolation.
    assert "private" not in json.dumps(result)
    assert not any(b"untouched-qa" in body for _, _, body in calls)
    assert all(method == "GET" or path == "/api/auth/session" for method, path, _ in calls)

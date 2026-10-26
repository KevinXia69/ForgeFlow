"""Exercise the same-origin reviewer proxy with real independent auth cookies."""

from __future__ import annotations

import copy
import hashlib
import json
from http.cookies import SimpleCookie

import httpx
import pytest
from fastapi.testclient import TestClient

from forgeflow.api.auth import issue_cookie, verify_cookie
from forgeflow.api.settings import Settings
from forgeflow.judge_gateway import (
    MAX_BODY,
    ROUTE_COOKIE,
    SESSION_COOKIE,
    configuration,
    create_app,
    issue_route,
)

HOST = "judges.example.test"
OTHER_HOST = "alternate.example.test"
INVITES = {"alice": "alice-invite-for-tests", "bob": "bob-invite-for-tests"}
PORTS = {"alice": 8101, "bob": 8102}


class WireStream(httpx.AsyncByteStream):
    """Unconsumed transport stream, unlike Response(content=...) in a mock."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False
        self.iterations = 0

    async def __aiter__(self):
        self.iterations += 1
        assert self.iterations == 1, "upstream stream was consumed twice"
        for chunk in self.chunks:
            yield chunk

    async def aclose(self):
        self.closed = True


class Backends:
    """Two loopback services sharing no session secret or invite code."""

    def __init__(self):
        self.settings = {
            name: Settings(session_secret=f"{name}-independent-session-secret-" * 2,
                           invite_codes=(invite,), trusted_hosts=(HOST, OTHER_HOST))
            for name, invite in INVITES.items()
        }
        self.calls = []
        self.streams = []
        self.failure = None
        self.login_mode = "normal"
        self.secure_session = True

    def response(self, status, data=None, *, headers=(), chunks=None):
        if chunks is None:
            chunks = [json.dumps(data).encode()] if data is not None else []
        stream = WireStream(chunks)
        self.streams.append(stream)
        return httpx.Response(status, headers=headers, stream=stream)

    async def __call__(self, request):
        self.calls.append(request)
        assert request.url.host == "127.0.0.1"
        assert request.url.scheme == "http"
        name = next(name for name, port in PORTS.items() if request.url.port == port)
        if self.failure == "connect":
            raise httpx.ConnectError("isolated backend unavailable", request=request)
        if self.failure == "503":
            return self.response(503, {"tenant": name, "unavailable": True})
        settings = self.settings[name]
        cookies = SimpleCookie(request.headers.get("cookie", ""))
        session = cookies[SESSION_COOKIE].value if SESSION_COOKIE in cookies else None
        principal = verify_cookie(settings, session, audience=request.headers.get("host"))
        if request.url.path == "/api/auth/session" and request.method == "POST":
            invite = json.loads(request.content)["invite_code"]
            assert invite == INVITES[name], "gateway selected the wrong isolated environment"
            if self.login_mode == "reject":
                return self.response(401, {"detail": "rejected"}, headers=[
                    ("set-cookie", "ff_session=; Max-Age=0; Path=/; Secure"),
                    ("set-cookie", f"{ROUTE_COOKIE}=malicious; Path=/; Secure"),
                ])
            if self.login_mode in {"missing", "empty"}:
                headers = [] if self.login_mode == "missing" else [("set-cookie", "ff_session=")]
                return self.response(201, {}, headers=headers)
            session = issue_cookie(settings, invite, audience=request.headers["host"])
            secure = "; Secure" if self.secure_session else ""
            return self.response(201, {"tenant": name}, headers=[
                ("set-cookie", f"ff_session={session}; Path=/{secure}; HttpOnly; SameSite=Lax"),
                ("set-cookie", "first=one; Path=/; Secure"),
                ("set-cookie", "second=two; Path=/; Secure"),
            ])
        if request.url.path == "/api/auth/session" and request.method == "DELETE":
            # The gateway must clear both auth cookies even if the backend omits them.
            return self.response(204)
        if request.url.path.startswith("/api/") and request.url.path not in {
            "/api/config", "/api/health"
        } and principal is None:
            return self.response(401, {"detail": "backend authentication required"})
        if request.url.path == "/api/events":
            return self.response(200, chunks=[b"event: delta\ndata: ", "你好".encode(), b"\n\n"],
                                 headers=[("content-type", "text/event-stream"),
                                          ("set-cookie", "one=1; Path=/"),
                                          ("set-cookie", "two=2; Path=/"),
                                          ("connection", "keep-alive, x-private-hop"),
                                          ("x-private-hop", "remove-me")])
        return self.response(200, {"tenant": name, "principal": principal},
                             headers=[("content-type", "application/json")])


@pytest.fixture
def rig():
    value = {"routing_secret": "gateway-only-routing-secret-" * 2,
             "trusted_hosts": [HOST, OTHER_HOST], "public_tenant": "alice",
             "tenants": [{"id": name, "port": PORTS[name],
                          "invite_sha256": [hashlib.sha256(invite.encode()).hexdigest()]}
                         for name, invite in INVITES.items()]}
    backends = Backends()
    app = create_app(value, transport=httpx.MockTransport(backends))
    with TestClient(app, base_url=f"https://{HOST}") as client:
        yield client, backends, value


def login(client, name="alice"):
    response = client.post("/api/auth/session", json={"invite_code": INVITES[name]})
    assert response.status_code == 201
    return response


def auth_header(route, session):
    return {"cookie": f"{ROUTE_COOKIE}={route}; {SESSION_COOKIE}={session}"}


def test_invites_share_one_url_but_use_independent_sessions_and_environments(rig):
    client, backends, _ = rig
    alice = login(client)
    alice_session = client.cookies.get(SESSION_COOKIE)
    alice_route = client.cookies.get(ROUTE_COOKIE)
    bob = login(client, "bob")
    bob_session = client.cookies.get(SESSION_COOKIE)
    assert str(alice.request.url) == str(bob.request.url) == f"https://{HOST}/api/auth/session"
    assert alice_session != bob_session
    assert verify_cookie(backends.settings["alice"], bob_session, audience=HOST) is None
    assert verify_cookie(backends.settings["bob"], alice_session, audience=HOST) is None
    assert client.get("/api/private").json()["tenant"] == "bob"
    response = client.get("/api/private", headers=auth_header(alice_route, alice_session))
    assert response.json()["tenant"] == "alice"
    assert response.json()["principal"] is not None
    assert ROUTE_COOKIE not in backends.calls[-1].headers.get("cookie", "")
    assert all(stream.closed for stream in backends.streams)


@pytest.mark.parametrize("secure_session", [True, False])
def test_public_requests_never_inherit_shared_client_or_unrouted_session(rig, secure_session):
    client, backends, _ = rig
    backends.secure_session = secure_session
    login(client)
    session = client.cookies.get(SESSION_COOKIE)
    client.cookies.clear()
    assert client.get("/api/config").json()["principal"] is None
    assert "cookie" not in backends.calls[-1].headers
    assert client.get("/api/config", headers={"cookie": f"ff_session={session}"}).json()[
        "principal"] is None
    assert "cookie" not in backends.calls[-1].headers


@pytest.mark.parametrize("method,path,status,forwarded", [
    ("GET", "/", 200, True), ("GET", "/app", 200, True),
    ("HEAD", "/api/health", 200, True), ("GET", "/api/config", 200, True),
    ("GET", "/api/private", 401, False), ("POST", "/api/config", 401, False),
    ("POST", "/anything", 401, False), ("GET", "/api%2Fprivate", 401, False),
])
def test_anonymous_access_is_limited(rig, method, path, status, forwarded):
    client, backends, _ = rig
    assert client.request(method, path).status_code == status
    assert bool(backends.calls) is forwarded


@pytest.mark.parametrize("headers,status", [
    ({"host": "attacker.example"}, 400),
    ({"origin": "https://attacker.example"}, 403),
    ({"origin": f"http://{HOST}"}, 403),
    ({"origin": "null"}, 403),
    ({"origin": "https://[malformed"}, 403),
    ({"origin": f"https://{HOST}/not-an-origin"}, 403),
    ({"sec-fetch-site": "cross-site"}, 403),
])
def test_host_and_browser_origin_fences(rig, headers, status):
    client, backends, _ = rig
    response = client.post("/api/auth/session", headers=headers,
                           json={"invite_code": INVITES["alice"]})
    assert response.status_code == status
    assert backends.calls == []


@pytest.mark.parametrize("change", ["forged", "session", "expired", "host", "tenant"])
def test_invalid_routing_claims_fail_closed_before_contacting_backend(rig, change):
    client, backends, value = rig
    login(client)
    session = client.cookies.get(SESSION_COOKIE)
    route = client.cookies.get(ROUTE_COOKIE)
    host = HOST
    if change == "forged":
        route = route[:-1] + ("A" if route[-1] != "A" else "B")
    elif change == "session":
        session = issue_cookie(backends.settings["bob"], INVITES["bob"], audience=HOST)
    elif change == "expired":
        route = issue_route(value, "alice", session, HOST, now=0)
    elif change == "host":
        host = OTHER_HOST
    else:
        route = issue_route(value, "http://attacker.example:9000", session, HOST)
    before = len(backends.calls)
    response = client.get("/api/private", headers={**auth_header(route, session), "host": host})
    assert response.status_code == 401
    assert len(backends.calls) == before


def test_backend_still_rejects_other_tenant_session_even_with_valid_gateway_signature(rig):
    client, backends, value = rig
    bob_session = issue_cookie(backends.settings["bob"], INVITES["bob"], audience=HOST)
    route = issue_route(value, "alice", bob_session, HOST)
    response = client.get("/api/private", headers=auth_header(route, bob_session))
    assert response.status_code == 401
    assert [call.url.port for call in backends.calls] == [PORTS["alice"]]


@pytest.mark.parametrize("bad_invite", [True, False])
def test_failed_login_keeps_existing_route_and_session(rig, bad_invite):
    client, backends, _ = rig
    login(client)
    before = dict(client.cookies)
    backends.login_mode = "reject"
    response = client.post("/api/auth/session", json={
        "invite_code": "not-configured" if bad_invite else INVITES["bob"]})
    assert response.status_code == 401
    assert dict(client.cookies) == before
    assert client.get("/api/private").json()["tenant"] == "alice"


@pytest.mark.parametrize("mode", ["missing", "empty"])
def test_incomplete_login_response_does_not_replace_identity(rig, mode):
    client, backends, _ = rig
    login(client)
    before = dict(client.cookies)
    backends.login_mode = mode
    assert client.post("/api/auth/session", json={"invite_code": INVITES["bob"]}).status_code == 502
    assert dict(client.cookies) == before
    assert backends.streams[-1].closed


@pytest.mark.parametrize("authenticated", [True, False])
def test_logout_clears_both_auth_cookies(rig, authenticated):
    client, backends, _ = rig
    if authenticated:
        login(client)
    before = len(backends.calls)
    response = client.delete("/api/auth/session")
    assert response.status_code == 204
    for name in (ROUTE_COOKIE, SESSION_COOKIE):
        cookies = [c for c in response.headers.get_list("set-cookie") if c.startswith(name + "=")]
        assert len(cookies) == 1
        assert "Max-Age=0" in cookies[0] and "Secure" in cookies[0]
        assert name not in client.cookies
    assert len(backends.calls) == before + int(authenticated)


def test_repeated_set_cookie_sse_and_encoded_query_survive_proxy(rig):
    client, backends, _ = rig
    response = login(client)
    assert len(response.headers.get_list("set-cookie")) == 4
    query = "?filter=a%2Fb%26c&same=1&same=2&q=%E4%B8%AD+word"
    with client.stream("GET", "/api/events" + query) as response:
        assert response.status_code == 200
        assert response.headers["content-type"] == "text/event-stream"
        assert b"".join(response.iter_bytes()) == b"event: delta\ndata: " + "你好".encode() + b"\n\n"
        assert response.headers.get_list("set-cookie") == ["one=1; Path=/", "two=2; Path=/"]
        assert "connection" not in response.headers
        assert "x-private-hop" not in response.headers
    assert backends.calls[-1].url.query == query[1:].encode()
    assert backends.streams[-1].closed and backends.streams[-1].iterations == 1


def test_forwarding_headers_cannot_change_identity_or_destination(rig):
    client, backends, _ = rig
    login(client, "bob")
    response = client.get("/api/private?upstream=http%3A%2F%2Fattacker.example", headers={
        "origin": f"https://{HOST}", "sec-fetch-site": "same-origin",
        "connection": "keep-alive, x-hidden, host, origin, cookie, sec-fetch-site",
        "x-hidden": "remove", "x-forwarded-for": "attacker",
        "x-forwarded-host": "attacker.example", "x-forwarded-port": "666",
        "x-forwarded-proto": "http", "forwarded": "host=attacker.example",
        "x-real-ip": "attacker", "accept-encoding": "gzip",
    })
    assert response.status_code == 200
    assert response.json()["tenant"] == "bob" and response.json()["principal"]
    request = backends.calls[-1]
    assert request.url.host == "127.0.0.1" and request.url.port == PORTS["bob"]
    assert request.headers["host"] == HOST
    assert request.headers["origin"] == f"https://{HOST}"
    assert request.headers["sec-fetch-site"] == "same-origin"
    assert request.headers["x-forwarded-for"] == "testclient"
    assert request.headers["x-forwarded-proto"] == "https"
    assert request.headers["accept-encoding"] == "identity"
    for name in ("connection", "x-hidden", "x-forwarded-host", "x-forwarded-port",
                 "forwarded", "x-real-ip"):
        assert name not in request.headers


@pytest.mark.parametrize("headers", [
    [("host", HOST), ("host", "attacker.example")],
    [("origin", f"https://{HOST}"), ("origin", "https://attacker.example")],
    [("cookie", "ff_session=a"), ("cookie", "ff_session=b")],
    [("cookie", "ff_session=a; ff_session=b")],
    [("cookie", f"{ROUTE_COOKIE}=a; {ROUTE_COOKIE}=b")],
])
def test_ambiguous_security_headers_are_rejected(rig, headers):
    client, backends, _ = rig
    assert client.get("/api/config", headers=headers).status_code == 400
    assert backends.calls == []


@pytest.mark.parametrize("failure", ["connect", "503"])
def test_backend_failure_does_not_fall_back_to_public_environment(rig, failure):
    client, backends, _ = rig
    login(client, "bob")
    backends.calls.clear()
    backends.failure = failure
    response = client.get("/api/private")
    assert response.status_code == 503
    assert [request.url.port for request in backends.calls] == [PORTS["bob"]]
    assert client.cookies.get(ROUTE_COOKIE)


def test_streamed_request_body_limit_enforced_before_forwarding(rig):
    client, backends, _ = rig
    login(client)
    backends.calls.clear()
    assert client.post("/api/private", content=b"x" * MAX_BODY).status_code == 200
    assert len(backends.calls[-1].content) == MAX_BODY
    before = len(backends.calls)
    response = client.post("/api/private", content=iter([b"x" * MAX_BODY, b"extra"]))
    assert response.status_code == 413
    assert len(backends.calls) == before


@pytest.mark.parametrize("mutation", ["url", "same_port", "duplicate_invite", "unknown_public"])
def test_configuration_rejects_arbitrary_destinations_and_ambiguous_mapping(rig, mutation):
    _, _, value = rig
    value = copy.deepcopy(value)
    if mutation == "url":
        value["tenants"][0]["upstream"] = "https://attacker.example"
    elif mutation == "same_port":
        value["tenants"][1]["port"] = value["tenants"][0]["port"]
    elif mutation == "duplicate_invite":
        value["tenants"][1]["invite_sha256"] = value["tenants"][0]["invite_sha256"]
    else:
        value["public_tenant"] = "unknown"
    with pytest.raises(ValueError):
        configuration(value)

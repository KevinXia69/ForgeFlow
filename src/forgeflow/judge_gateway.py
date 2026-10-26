"""Same-origin routing to fixed, independently authenticated reviewer instances.

This process has invite hashes and its own routing key, but no backend session
secrets, model key, ERP credentials or executor access. Each backend still
authenticates every request. A route cookie is bound to the backend session;
it cannot select a client-supplied address or silently fall back to another ERP.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import re
import time
from contextlib import asynccontextmanager
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse

from forgeflow.api.rate_limit import FixedWindowLimiter
from forgeflow.api.settings import Settings
from forgeflow.api.trust import is_trusted_api_request

ROUTE_COOKIE = "__Host-ff_route"
SESSION_COOKIE = "ff_session"
MAX_BODY = 1024 * 1024
HOP_HEADERS = frozenset({
    b"connection", b"keep-alive", b"proxy-authenticate", b"proxy-authorization",
    b"te", b"trailer", b"transfer-encoding", b"upgrade",
})
SECURITY_HEADERS = {"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"}


def configuration(value: dict) -> dict:
    if not isinstance(value, dict):
        raise ValueError("invalid_gateway_configuration")
    result = dict(value)
    key = value.get("routing_secret")
    tenants = value.get("tenants")
    if not isinstance(key, str) or len(key) < 32 or not isinstance(tenants, list):
        raise ValueError("invalid_gateway_configuration")
    if not 1 <= len(tenants) <= 32:
        raise ValueError("invalid_gateway_configuration")
    names, ports, hashes = set(), set(), set()
    for tenant in tenants:
        if not isinstance(tenant, dict) or set(tenant) != {"id", "port", "invite_sha256"}:
            raise ValueError("invalid_gateway_tenant")
        name, port, invites = tenant["id"], tenant["port"], tenant["invite_sha256"]
        if (not isinstance(name, str) or re.fullmatch(r"[a-z][a-z0-9-]{0,31}", name) is None
                or name in names or type(port) is not int or not 1024 <= port <= 65535
                or port in ports or not isinstance(invites, list) or not invites):
            raise ValueError("invalid_gateway_tenant")
        names.add(name)
        ports.add(port)
        for digest in invites:
            if (not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None
                    or digest in hashes):
                raise ValueError("duplicate_or_invalid_invite_hash")
            hashes.add(digest)
    if value.get("public_tenant") not in names:
        raise ValueError("invalid_public_tenant")
    # Reuse the application's exact Host and browser-origin checks. These dummy
    # authentication values are never used to issue or authenticate a session.
    result["trust"] = Settings(session_secret="unused-trust-validation-" * 2,
        invite_codes=("unused-trust-validation",), deployment="hosted",
        trusted_hosts=tuple(value.get("trusted_hosts", [])), environment_generation="0" * 32)
    return result


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def issue_route(config: dict, tenant: str, session: str, host: str, *, now=None) -> str:
    payload = _encode(json.dumps({"v": 1, "tenant": tenant,
        "session": hashlib.sha256(session.encode()).hexdigest(), "host": host,
        "exp": int(time.time() if now is None else now) + 12 * 60 * 60},
        separators=(",", ":")).encode())
    signature = _encode(hmac.new(config["routing_secret"].encode(), payload.encode(),
                                 hashlib.sha256).digest())
    return payload + "." + signature


def read_route(config: dict, cookie: str, session: str, host: str, *, now=None) -> str | None:
    try:
        if not cookie or len(cookie) > 2048 or not session or len(session) > 4096:
            return None
        payload, signature = cookie.split(".")
        expected = _encode(hmac.new(config["routing_secret"].encode(), payload.encode(),
                                    hashlib.sha256).digest())
        if not hmac.compare_digest(expected, signature):
            return None
        raw = base64.b64decode(payload + "=" * (-len(payload) % 4), altchars=b"-_", validate=True)
        value = json.loads(raw)
        if (not isinstance(value, dict) or type(value.get("v")) is not int or value["v"] != 1
                or type(value.get("exp")) is not int
                or value["exp"] <= int(time.time() if now is None else now)
                or value.get("host") != host
                or value.get("session") != hashlib.sha256(session.encode()).hexdigest()
                or value.get("tenant") not in {row["id"] for row in config["tenants"]}):
            return None
        return value["tenant"]
    except (ValueError, TypeError, UnicodeError, AttributeError):
        return None


def _hop_headers(raw: list[tuple[bytes, bytes]]) -> set[bytes]:
    # Connection can nominate additional hop-by-hop fields.
    result = set(HOP_HEADERS)
    for key, value in raw:
        if key.lower() == b"connection":
            result.update(part.strip().lower() for part in value.split(b","))
    return result


def create_app(value: dict, *, transport=None) -> FastAPI:
    config = configuration(value)
    tenants = {row["id"]: row for row in config["tenants"]}
    limiter = FixedWindowLimiter()

    @asynccontextmanager
    async def lifespan(app):
        async with httpx.AsyncClient(transport=transport, trust_env=False,
                follow_redirects=False, timeout=httpx.Timeout(960, connect=5),
                limits=httpx.Limits(max_connections=128, max_keepalive_connections=32)) as client:
            app.state.client = client
            yield

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)

    def error(detail, status, **headers):
        return JSONResponse({"detail": detail}, status_code=status,
                            headers={**SECURITY_HEADERS, **headers})

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"])
    async def proxy(request: Request, path: str):
        # Do not let the gateway and backend choose different values from
        # duplicated security headers or authentication cookies.
        for name in ("host", "origin", "sec-fetch-site", "cookie"):
            if len(request.headers.getlist(name)) > 1:
                return error("ambiguous request headers", 400)
        cookie_header = request.headers.get("cookie", "")
        for name in (ROUTE_COOKIE, SESSION_COOKIE):
            if sum(part.strip().split("=", 1)[0] == name
                   for part in cookie_header.split(";")) > 1:
                return error("ambiguous authentication cookies", 400)
        host = request.headers.get("host", "")
        if not config["trust"].allows_host(host):
            return error("untrusted host", 400)
        api = request.url.path.startswith("/api")
        if api:
            try:
                trusted = is_trusted_api_request(request, config["trust"])
                if request.headers.get("origin"):
                    origin = urlsplit(request.headers["origin"])
                    trusted = trusted and not (
                        origin.scheme != request.url.scheme or origin.path or origin.query
                        or origin.fragment or origin.username is not None)
            except ValueError:
                trusted = False
            if not trusted:
                return error("untrusted request", 403)
        login = request.url.path == "/api/auth/session" and request.method == "POST"
        logout = request.url.path == "/api/auth/session" and request.method == "DELETE"
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > MAX_BODY:
                return error("request_body_too_large", 413)
        session = request.cookies.get(SESSION_COOKIE, "")
        tenant = read_route(config, request.cookies.get(ROUTE_COOKIE, ""), session, host)
        authenticated_route = tenant is not None
        if login:
            client_ip = request.client.host if request.client else "unknown"
            for identity, limit in (("login:" + client_ip, 10), ("login:global", 100)):
                delay = limiter.retry_after(identity, limit=limit)
                if delay:
                    return error("rate_limit_exceeded", 429, **{"Retry-After": str(delay)})
            try:
                login_body = json.loads(body)
                invite = login_body.get("invite_code")
                if not isinstance(invite, str) or not 1 <= len(invite) <= 512:
                    raise ValueError
            except (ValueError, AttributeError, UnicodeError):
                return error("invalid_login_request", 422)
            candidate = hashlib.sha256(invite.encode()).hexdigest()
            tenant = None
            for row in config["tenants"]:
                for expected in row["invite_sha256"]:
                    if hmac.compare_digest(candidate, expected):
                        tenant = row["id"]
            if tenant is None:
                return error("invalid invite code", 401)
        if tenant is None:
            if logout:
                response = Response(status_code=204, headers=SECURITY_HEADERS)
                for name in (ROUTE_COOKIE, SESSION_COOKIE):
                    response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")
                return response
            public = (request.method in {"GET", "HEAD"} and (
                not api or request.url.path in {"/api/config", "/api/health"}))
            if not public:
                return error("authentication required; sign in again", 401)
            tenant = config["public_tenant"]
        raw_headers = list(request.headers.raw)
        excluded = _hop_headers(raw_headers) | {b"content-length", b"forwarded", b"x-real-ip",
            b"host", b"origin", b"sec-fetch-site", b"cookie", b"accept-encoding"}
        headers = [(key, val) for key, val in raw_headers if key.lower() not in excluded
                   and not key.lower().startswith(b"x-forwarded-")]
        # These checked security fields cannot be suppressed by Connection's
        # additional hop-by-hop header names. The route cookie stays here.
        for name in ("host", "origin", "sec-fetch-site"):
            if name in request.headers:
                headers.append((name.encode(), request.headers[name].encode("latin-1")))
        if authenticated_route and not login:
            headers.append((b"cookie", f"{SESSION_COOKIE}={session}".encode("latin-1")))
        # Preserve Host and Origin for the backend's audience/CSRF checks, but
        # replace client-supplied forwarding metadata and disable compression.
        headers.extend([(b"x-forwarded-proto", b"https"), (b"accept-encoding", b"identity"),
            (b"x-forwarded-for", (request.client.host if request.client else "127.0.0.1").encode())])
        url = httpx.URL(scheme="http", host="127.0.0.1", port=tenants[tenant]["port"],
                       raw_path=request.scope.get("raw_path", request.url.path.encode()) +
                       (b"?" + request.scope["query_string"] if request.scope["query_string"] else b""))
        try:
            # build_request merges the client's shared cookie jar, which would
            # leak one reviewer's backend session into another user's request.
            # A standalone request forwards only this request's validated cookie.
            upstream = await app.state.client.send(httpx.Request(
                request.method, url, headers=headers, content=bytes(body)), stream=True)
        except httpx.HTTPError:
            return error("reviewer_environment_temporarily_unavailable", 503,
                         **{"Retry-After": "10"})
        returned = SimpleCookie()
        try:
            for cookie in upstream.headers.get_list("set-cookie"):
                returned.load(cookie)
        except CookieError:
            await upstream.aclose()
            return error("reviewer_session_unavailable", 502)
        if login and upstream.status_code == 201:
            if (SESSION_COOKIE not in returned or not returned[SESSION_COOKIE].value
                    or len(returned[SESSION_COOKIE].value) > 4096):
                await upstream.aclose()
                return error("reviewer_session_unavailable", 502)
        response = StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                                     background=BackgroundTask(upstream.aclose))
        excluded = _hop_headers(list(upstream.headers.raw))
        response.raw_headers = []
        for key, val in upstream.headers.raw:
            if key.lower() in excluded:
                continue
            if key.lower() == b"set-cookie":
                name = val.split(b"=", 1)[0].strip().decode("latin-1")
                if name == ROUTE_COOKIE or (name == SESSION_COOKIE and (
                        login and upstream.status_code != 201 or
                        logout and upstream.status_code == 204)):
                    continue
            response.raw_headers.append((key, val))
        if login and upstream.status_code == 201:
            route = issue_route(config, tenant, returned[SESSION_COOKIE].value, host)
            response.set_cookie(ROUTE_COOKIE, route, max_age=12 * 60 * 60, path="/",
                                secure=True, httponly=True, samesite="lax")
        if logout and upstream.status_code == 204:
            for name in (ROUTE_COOKIE, SESSION_COOKIE):
                response.delete_cookie(name, path="/", secure=True, httponly=True, samesite="lax")
        return response

    return app


def main() -> int:
    import uvicorn

    from forgeflow.vps_runtime import trusted_json

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    app = create_app(trusted_json(args.config))
    uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=True,
                forwarded_allow_ips="127.0.0.1", access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

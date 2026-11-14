"""Signed invite sessions with durable logout revocation."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import HTTPException, Request, Response

from .settings import Settings

UID = re.compile(r"[0-9a-f]{26}")
#: The account session cookie is separate from the legacy invite cookie.
ACCOUNT_COOKIE = "forgeflow_account"
MAX_COOKIE_LEN = 4096
Now = datetime | float | int | None


class InvalidInviteCode(ValueError):
    """The supplied invite code matches no configured code."""


def _session_token_hash(value: str) -> str:
    # The signature's equivalent base64 padding must not create a second token
    # identity that evades revocation. The signed payload is exact HMAC input.
    return hashlib.sha256(value.partition(".")[0].encode()).hexdigest()


class InviteSessionRevocations:
    """Store only revoked token hashes until their signed expiry.

    Connections are per operation so HTTP threads and API workers share the
    same SQLite transaction boundary. Removing an expired row never revives a
    session: signature verification rejects its expiry first.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with self._connection() as connection:
            connection.execute("CREATE TABLE IF NOT EXISTS revoked_sessions ("
                               "token_hash TEXT PRIMARY KEY, expires_at INTEGER NOT NULL)")
            connection.execute("CREATE INDEX IF NOT EXISTS revoked_expiry "
                               "ON revoked_sessions(expires_at)")
            connection.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (_epoch(None),))
        path.chmod(0o600)

    @contextmanager
    def _connection(self):
        connection = sqlite3.connect(self.path, timeout=10)
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def revoked(self, value: str) -> bool:
        with self._connection() as connection:
            row = connection.execute("SELECT 1 FROM revoked_sessions WHERE token_hash=?",
                                     (_session_token_hash(value),)).fetchone()
        return row is not None

    def revoke(self, settings: Settings, value: str | None, *, audience: str) -> None:
        if verify_cookie(settings, value, audience=audience) is None:
            return
        payload = json.loads(_decode(value.split(".", 1)[0]))
        with self._connection() as connection:
            connection.execute("DELETE FROM revoked_sessions WHERE expires_at <= ?", (_epoch(None),))
            connection.execute("INSERT OR IGNORE INTO revoked_sessions(token_hash, expires_at) "
                               "VALUES (?, ?)",
                               (_session_token_hash(value), payload["exp"]))


def end_browser_session(request: Request, response: Response) -> Response:
    """Revoke both supported cookies; repeated logout is harmless.

    The response confirms logout only after durable revocation succeeds. A
    storage failure leaves the browser able to retry instead of claiming that
    deleting a cookie invalidated an otherwise reusable server session.
    """
    settings = request.app.state.settings
    expected = request.headers.get("x-forgeflow-workspace")
    if expected is not None:
        principal = optional_principal(request)
        if principal is not None and expected != workspace_scope(settings, principal):
            # A stale tab must not revoke the replacement identity's cookie.
            # Missing/expired identity still permits idempotent cookie cleanup.
            raise HTTPException(status_code=409, detail="workspace_changed",
                                headers={"X-ForgeFlow-Workspace-Changed": "1"})
    accounts = getattr(request.app.state, "accounts", None)
    if accounts is not None:
        accounts.logout(request.cookies.get(ACCOUNT_COOKIE))
    revocations = getattr(request.app.state, "invite_revocations", None)
    if revocations is not None:
        try:
            revocations.revoke(settings, request.cookies.get(settings.cookie_name),
                               audience=request.headers.get("host") or request.url.netloc)
        except (sqlite3.Error, OSError):
            raise HTTPException(status_code=503, detail="session_revocation_unavailable") from None
    response.delete_cookie(settings.cookie_name, path="/", secure=settings.secure_cookie,
                           httponly=True, samesite="lax")
    response.delete_cookie(ACCOUNT_COOKIE, path="/", secure=settings.secure_cookie,
                           httponly=True, samesite="strict")
    response.status_code = 204
    return response


def _epoch(now: Now) -> int:
    if now is None:
        return int(time.time())
    return int(now.timestamp()) if isinstance(now, datetime) else int(now)


def session_expiry(settings: Settings, *, now: Now = None) -> int:
    """Return the absolute expiry stamped into a newly issued session."""
    return _epoch(now) + settings.session_ttl_sec


def _encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes | None:
    if not value or len(value) > MAX_COOKIE_LEN:
        return None
    try:
        return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)
    except ValueError:
        return None


def _matches(settings: Settings, invite_code: str) -> bool:
    if not isinstance(invite_code, str):
        return False
    candidate = hashlib.sha256(invite_code.encode()).digest()
    matched = False
    for configured in settings.invite_codes:  # compare every code, never stop early
        expected = hashlib.sha256(configured.encode()).digest()
        matched |= hmac.compare_digest(candidate, expected)
    return matched


def _identity(settings: Settings, invite_code: str) -> str:
    code_digest = hashlib.sha256(invite_code.encode()).hexdigest()
    message = f"uid:{code_digest}".encode("ascii")
    return hmac.new(
        settings.session_secret.encode(), message, hashlib.sha256
    ).hexdigest()[:26]


def _signature(settings: Settings, payload: str) -> bytes:
    return hmac.new(
        settings.session_secret.encode(), payload.encode(), hashlib.sha256
    ).digest()


def issue_cookie(
    settings: Settings, invite_code: str, *, now: Now = None, audience: str | None = None,
) -> str:
    """Return a signed ``payload.signature`` cookie for a valid invite code."""
    if not _matches(settings, invite_code):
        raise InvalidInviteCode("invite code is not valid")
    claims: dict[str, object] = {
        "v": 1,
        "uid": _identity(settings, invite_code),
        "exp": session_expiry(settings, now=now),
        "sid": secrets.token_hex(16),
    }
    if isinstance(audience, str) and audience:
        claims["aud"] = audience[:255]
    payload = _encode(
        json.dumps(claims, separators=(",", ":")).encode("ascii")
    )
    return f"{payload}.{_encode(_signature(settings, payload))}"


def verify_cookie(
    settings: Settings, value: str | None, *, now: Now = None, audience: str | None = None,
) -> str | None:
    """Return the uid of a valid, unexpired cookie, or None for any failure."""
    if not isinstance(value, str) or len(value) > MAX_COOKIE_LEN:
        return None
    payload, separator, signature = value.partition(".")
    if not separator or not payload or not signature:
        return None
    supplied = _decode(signature)
    if supplied is None or not hmac.compare_digest(_signature(settings, payload), supplied):
        return None
    raw = _decode(payload)
    if raw is None:
        return None
    try:
        claims: Any = json.loads(raw)
    except ValueError:
        return None
    if not isinstance(claims, dict) or type(claims.get("v")) is not int or claims["v"] != 1:
        return None
    uid = claims.get("uid")
    expiry = claims.get("exp")
    if not isinstance(uid, str) or UID.fullmatch(uid) is None:
        return None
    if type(expiry) is not int or expiry <= _epoch(now):
        return None
    # Removing an invite must revoke its already-issued sessions as soon as
    # the application reloads the configuration, not only future logins.
    active = False
    for invite_code in settings.invite_codes:
        active |= hmac.compare_digest(uid, _identity(settings, invite_code))
    if not active:
        return None
    recorded_audience = claims.get("aud")
    if audience is not None:
        if not isinstance(recorded_audience, str) or not hmac.compare_digest(
            recorded_audience, audience[:255]
        ):
            return None
    elif recorded_audience is not None:
        return None
    return uid


def optional_principal(request: Request) -> str | None:
    """Return the authenticated principal, without raising.

    When an account store is installed, only a revocable account session is a
    login identity.  The legacy invite cookie is not a fallback: an invite code
    creates an account, it does not authenticate one.  Deployments without the
    account store keep the development invite session.
    """
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        return None
    accounts = getattr(request.app.state, "accounts", None)
    if accounts is not None:
        verified = accounts.verify_session(request.cookies.get(ACCOUNT_COOKIE))
        if verified is None:
            return None
        account, _session_id = verified
        return account.principal_id
    value = request.cookies.get(settings.cookie_name)
    host = request.headers.get("host") or request.url.netloc
    principal = verify_cookie(settings, value, audience=host) if value else None
    if principal is None:
        return None
    revocations = getattr(request.app.state, "invite_revocations", None)
    if revocations is not None:
        try:
            if revocations.revoked(value):
                return None
        except (sqlite3.Error, OSError):
            return None  # An unavailable denylist must not restore revoked access.
    return principal


def workspace_scope(settings: Settings, uid: str) -> str:
    material = f"{settings.deployment}\0{settings.data_root.resolve()}\0{uid}"
    return hmac.new(settings.session_secret.encode(), material.encode(), hashlib.sha256).hexdigest()


def require_principal(request: Request) -> str:
    """FastAPI dependency returning the cookie's uid or raising 401."""
    uid = optional_principal(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="authentication required")
    expected = request.headers.get("x-forgeflow-workspace")
    # Native EventSource cannot attach a custom header to the progress subscription.
    if expected is None and request.method == "GET" and request.url.path.endswith("/events"):
        expected = request.query_params.get("workspace_scope")
    if expected is not None and expected != workspace_scope(request.app.state.settings, uid):
        raise HTTPException(status_code=409, detail="workspace_changed",
                            headers={"X-ForgeFlow-Workspace-Changed": "1"})
    return uid

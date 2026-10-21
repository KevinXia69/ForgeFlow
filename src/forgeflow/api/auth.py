"""Stateless invite-code sessions backed by HMAC-signed cookies."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from datetime import datetime
from typing import Any

from fastapi import HTTPException, Request

from .settings import Settings

UID = re.compile(r"[0-9a-f]{26}")
MAX_COOKIE_LEN = 4096
Now = datetime | float | int | None


class InvalidInviteCode(ValueError):
    """The supplied invite code matches no configured code."""


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
    """Return the cookie's uid when present and valid, without raising."""
    settings = getattr(request.app.state, "settings", None)
    if not isinstance(settings, Settings):
        return None
    value = request.cookies.get(settings.cookie_name)
    host = request.headers.get("host") or request.url.netloc
    return verify_cookie(settings, value, audience=host) if value else None


def require_principal(request: Request) -> str:
    """FastAPI dependency returning the cookie's uid or raising 401."""
    uid = optional_principal(request)
    if uid is None:
        raise HTTPException(status_code=401, detail="authentication required")
    return uid

"""Browser request trust for the ForgeFlow API.

A browser request is accepted only when it targets a loopback/configured
authority and its Origin (when present) is the same authority.  Non-browser
clients without Origin/Sec-Fetch-Site headers remain usable for integration
and health checks.
"""

from __future__ import annotations

from urllib.parse import urlsplit

from fastapi import Request

from .settings import Settings

_LOOPBACK = {"127.0.0.1", "::1", "localhost"}
_CROSS_SITE = "cross-site"


def _authority(value: str | None) -> str | None:
    if not value:
        return None
    raw = value.strip()
    if raw.startswith("["):
        # IPv6 authority: [::1]:8000 or [::1]
        close = raw.find("]")
        if close == -1:
            return None
        host = raw[1:close].lower()
        remainder = raw[close + 1 :]
        if remainder and not remainder.startswith(":"):
            return None
        port = remainder[1:]
    else:
        host, separator, port = raw.partition(":")
        host = host.lower()
        if not separator:
            port = ""
    if not host or ":" in host and not host.startswith("[") and host != "::1":
        # A non-bracket host containing ':' is an unbracketed IPv6 authority;
        # reject it rather than guessing where the port starts.
        if not (host.count(":") > 1 and host == "::1"):
            return None
    if port and not port.isdigit():
        return None
    if ":" in host:
        return f"[{host}]:{port}" if port else f"[{host}]"
    return f"{host}:{port}" if port else host


def _hostname(authority: str) -> str:
    if authority.startswith("["):
        close = authority.find("]")
        return authority[1:close] if close != -1 else authority
    return authority.rsplit(":", 1)[0] if ":" in authority else authority


def _is_trusted_authority(authority: str, settings: Settings) -> bool:
    hostname = _hostname(authority)
    if hostname in _LOOPBACK:
        return True
    return authority in settings.trusted_hosts or hostname in settings.trusted_hosts


def is_trusted_api_request(request: Request, settings: Settings) -> bool:
    """Return whether a browser request passes the Host/Origin fence."""
    site = (request.headers.get("sec-fetch-site") or "").strip().lower()
    origin = request.headers.get("origin")
    if site == _CROSS_SITE:
        return False
    # Plain API clients (curl, health checks, tests) do not send browser
    # provenance headers and are not subject to the browser-only fence.
    if not site and origin is None:
        return True
    host = _authority(request.headers.get("host") or request.url.netloc)
    if host is None or not _is_trusted_authority(host, settings):
        return False
    if origin is not None:
        if origin.strip().lower() == "null":
            return False
        parsed = urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or parsed.netloc == "":
            return False
        if _authority(parsed.netloc) != host:
            return False
    return True

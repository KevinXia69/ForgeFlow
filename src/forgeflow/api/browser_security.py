"""Staged browser policy and bounded, content-free violation telemetry.

Reports are attacker-controlled. Only fixed categories/counters are retained;
URLs, filenames, samples, account IDs, IPs and raw bodies are never recorded.
"""
from __future__ import annotations

import json
import logging
import threading
from collections import Counter
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response

from .rate_limit import FixedWindowLimiter

logger = logging.getLogger(__name__)
REPORT_PATH = "/api/browser-reports"
MAX_REPORT_BYTES = 16_384
MAX_REPORT_BATCH = 20
# Existing DENY framing intent is also expressed in CSP. Broad resource and
# Trusted Types restrictions are deliberately report-only during discovery.
ENFORCED_CSP = "base-uri 'self'; object-src 'none'; frame-ancestors 'none'"
REPORT_ONLY_CSP = "; ".join((
    "default-src 'self'", "script-src 'self'", "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob: https:", "font-src 'self' data:", "connect-src 'self'",
    "media-src 'self' blob:", "frame-src 'self'", "object-src 'none'",
    "base-uri 'self'", "form-action 'self'", "require-trusted-types-for 'script'",
    "trusted-types 'none'", "report-to forgeflow-csp", f"report-uri {REPORT_PATH}",
))
POLICY_HEADERS = {
    "Content-Security-Policy": ENFORCED_CSP,
    "Content-Security-Policy-Report-Only": REPORT_ONLY_CSP,
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}
_DIRECTIVES = frozenset({
    "default-src", "script-src", "script-src-elem", "script-src-attr",
    "style-src", "style-src-elem", "style-src-attr", "img-src", "font-src",
    "connect-src", "media-src", "frame-src", "object-src", "base-uri",
    "form-action", "frame-ancestors", "require-trusted-types-for", "trusted-types",
})


def report_category(body: object) -> tuple[str, str, str] | None:
    if not isinstance(body, dict):
        return None
    directive = body.get("effective-directive", body.get("effectiveDirective"))
    if not isinstance(directive, str) or directive not in _DIRECTIVES:
        return None
    disposition = body.get("disposition")
    if not isinstance(disposition, str) or disposition not in {"report", "enforce"}:
        disposition = "unknown"
    blocked = body.get("blocked-uri", body.get("blockedURL", ""))
    resource = "other"
    if isinstance(blocked, str):
        if blocked in {"inline", "eval", "trusted-types-sink", "trusted-types-policy"}:
            resource = blocked
        else:
            # Preserve scheme category only; even an origin can contain a
            # user-specific identifier. Do not log paths/query/sample fields.
            try:
                scheme = urlsplit(blocked).scheme.lower()
            except ValueError:
                scheme = ""
            if scheme in {"http", "https", "data", "blob"}:
                resource = scheme
    return directive, disposition, resource


class BrowserReports:
    """Fixed-key counters reset with this API process; no report-content store."""
    def __init__(self):
        self._counts: Counter[tuple[str, str, str]] = Counter()
        self._lock = threading.Lock()
        self.limiter = FixedWindowLimiter(max_keys=512)

    def record(self, category: tuple[str, str, str]) -> None:
        with self._lock:
            self._counts[category] = min(self._counts[category] + 1, 2**31 - 1)
        logger.info("browser_policy_violation directive=%s disposition=%s resource=%s", *category)

    def snapshot(self) -> list[dict[str, str | int]]:
        with self._lock:
            return [{"directive": d, "disposition": p, "resource": r, "count": count}
                    for (d, p, r), count in sorted(self._counts.items())]


def install_browser_report_route(app: FastAPI) -> None:
    reports = BrowserReports()
    app.state.browser_reports = reports

    @app.post(REPORT_PATH, include_in_schema=False)
    async def browser_report(request: Request):
        # Reports can originate on the login screen and do not require an
        # account. The outer Host/Origin fence still applies without exception.
        for key, limit in (("global", 120),
                           (f"client:{request.client.host if request.client else 'unknown'}", 30)):
            delay = reports.limiter.retry_after(key, limit=limit)
            if delay:
                return Response(status_code=429, headers={"Retry-After": str(delay)})
        if request.headers.get("content-type", "").split(";", 1)[0].strip() not in {
                "application/csp-report", "application/reports+json", "application/json"}:
            return Response(status_code=415)
        raw = bytearray()
        async for chunk in request.stream():
            if len(raw) + len(chunk) > MAX_REPORT_BYTES:
                return Response(status_code=413)
            raw.extend(chunk)
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError, RecursionError):
            return Response(status_code=400)
        if isinstance(value, dict) and "csp-report" in value:
            bodies = [value["csp-report"]]
        elif isinstance(value, list) and len(value) <= MAX_REPORT_BATCH:
            bodies = [item.get("body") for item in value
                      if isinstance(item, dict) and item.get("type") == "csp-violation"]
        else:
            return Response(status_code=400)
        for body in bodies:
            category = report_category(body)
            if category is not None:
                reports.record(category)
        return Response(status_code=204)

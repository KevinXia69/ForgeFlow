#!/usr/bin/env python3
"""A local review deployment: the public bundle in front of the real read-only API.

The public review bundle is built with base ``/app/`` and talks to ``/api`` on its
own origin. This serves the built bundle at ``/app/`` and proxies ``/api`` to a
real API running with ``FORGEFLOW_DEPLOYMENT=review``, so the review story can be
exercised end to end against a real backend instead of mocks.

It is a development and verification tool: single process, no TLS, no caching,
and it refuses to start unless the API reports the review deployment.

Usage:
    scripts/review_backend.py --api http://127.0.0.1:8091 --port 8080
    scripts/review_backend.py --api http://127.0.0.1:8091 --port 8080 --dist web-public/dist
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".woff2": "font/woff2",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def check_review_api(api: str) -> dict:
    """Refuse to serve a review front end against anything but a review API."""
    try:
        with urllib.request.urlopen(f"{api}/api/config", timeout=5) as response:
            config = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as error:
        raise SystemExit(f"the API at {api} is not reachable: {error}") from None
    deployment = config.get("deployment")
    if deployment != "review":
        raise SystemExit(f"the API at {api} reports deployment={deployment!r}; expected 'review'")
    return config


def build(api: str, dist: Path) -> type[BaseHTTPRequestHandler]:
    index = dist / "index.html"
    if not index.is_file():
        raise SystemExit(f"no built bundle at {dist}; run `npm run build` in web-public first")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002 - stdlib signature
            pass

        def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None) -> None:
            self.send_response(status)
            self.send_header("content-type", content_type)
            self.send_header("content-length", str(len(body)))
            for name, value in (extra or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if not self.command == "HEAD":
                self.wfile.write(body)

        def _file(self, relative: str) -> None:
            target = (dist / relative).resolve()
            if not str(target).startswith(str(dist.resolve())) or not target.is_file():
                self._send(404, b"not found", "text/plain; charset=utf-8")
                return
            self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))

        def _api(self) -> None:
            length = int(self.headers.get("content-length") or 0)
            payload = self.rfile.read(length) if length else None
            request = urllib.request.Request(
                f"{api}{self.path}",
                data=payload,
                method=self.command,
                headers={name: value for name, value in self.headers.items()
                         if name.lower() in {"content-type", "accept", "cookie", "authorization"}},
            )
            opener = urllib.request.build_opener()
            try:
                with opener.open(request, timeout=30) as response:
                    body, status, headers = response.read(), response.status, response.headers
            except urllib.error.HTTPError as error:
                body, status, headers = error.read(), error.code, error.headers
            except (urllib.error.URLError, OSError) as error:
                self._send(502, str(error).encode(), "text/plain; charset=utf-8")
                return
            extra = {}
            cookies = headers.get_all("set-cookie") or []
            for cookie in cookies:
                extra.setdefault("set-cookie", cookie)
            if len(cookies) > 1:
                extra["set-cookie"] = "\n".join(cookies)
            self._send(status, body, headers.get("content-type", "application/json"), extra)

        def do_GET(self) -> None:
            if self.path.startswith("/api/"):
                self._api()
            elif self.path in ("/", "/index.html"):
                self._send(302, b"", "text/plain", {"location": "/app/"})
            elif self.path.startswith("/app/"):
                self._file(self.path[len("/app/"):] or "index.html")
            else:
                self._file(self.path.lstrip("/"))

        def do_POST(self) -> None:
            self._api()

        def do_HEAD(self) -> None:
            self.do_GET()

    return Handler


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--api", default="http://127.0.0.1:8091")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--dist", type=Path, default=ROOT / "web-public" / "dist")
    args = parser.parse_args()

    config = check_review_api(args.api)
    handler = build(args.api, args.dist)
    server = ThreadingHTTPServer(("127.0.0.1", args.port), handler)
    print(f"review backend: http://127.0.0.1:{args.port}/app/  ->  api {args.api} (deployment={config.get('deployment')})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Bundled, build-free web UI served next to the read-only product API.

The UI is plain HTML/CSS/JS so it works offline and from an installed wheel.
``mount_web`` is called after ``create_app``; it must never shadow ``/api/*``.
"""

from __future__ import annotations

import hashlib
import os
from functools import lru_cache
from pathlib import Path

from fastapi import FastAPI
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import Headers
from starlette.responses import FileResponse
from starlette.staticfiles import NotModifiedResponse, StaticFiles

from .settings import Settings

STATIC_DIR = Path(__file__).with_name("static")
APP_DIR = STATIC_DIR / "app"


@lru_cache(maxsize=512)
def _content_etag(path: str, identity: tuple[int, ...]) -> str:
    # File metadata invalidates only this internal cache. It is never serialized
    # into a validator: touching/redeploying identical bytes keeps the same ETag.
    with open(path, "rb") as source:
        return '"' + hashlib.file_digest(source, "sha256").hexdigest() + '"'


class ContentFileResponse(FileResponse):
    """Static bytes with content validators, without filesystem timestamps."""

    def set_stat_headers(self, stat_result: os.stat_result) -> None:
        self.headers.setdefault("content-length", str(stat_result.st_size))

    def _should_use_range(self, http_if_range: str) -> bool:
        # No date-based validator exists. Weak validators cannot authorize range.
        return not http_if_range.startswith("W/") and http_if_range == self.headers.get("etag")

    async def __call__(self, scope, receive, send) -> None:
        if self.stat_result is None:
            self.stat_result = await run_in_threadpool(os.stat, self.path)
            self.set_stat_headers(self.stat_result)
        info = self.stat_result
        identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        self.headers["etag"] = await run_in_threadpool(_content_etag, str(self.path), identity)
        if "last-modified" in self.headers:
            del self.headers["last-modified"]
        match = Headers(scope=scope).get("if-none-match")
        if self.status_code == 200 and scope["method"] in {"GET", "HEAD"} and match:
            tags = [tag.strip().removeprefix("W/") for tag in match.split(",")]
            if "*" in tags or self.headers["etag"] in tags:
                await NotModifiedResponse(self.headers)(scope, receive, send)
                return
        await super().__call__(scope, receive, send)


class ContentStaticFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        # Conditional handling is deferred until the asynchronous content hash
        # is ready; StaticFiles' mtime-based conditional handling is bypassed.
        return ContentFileResponse(full_path, stat_result=stat_result, status_code=status_code)

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        # Starlette's optional HTML 404 fallback constructs FileResponse directly.
        if isinstance(response, FileResponse) and not isinstance(response, ContentFileResponse):
            return ContentFileResponse(response.path, stat_result=response.stat_result,
                                       status_code=response.status_code, media_type=response.media_type)
        return response


def mount_web(app: FastAPI, settings: Settings) -> None:
    """Serve the bundled single-page UI, last, without disturbing the API.

    ``settings`` is accepted so callers can mount the UI with the same
    configuration object used to build the app. When the static directory is
    absent (for example a slim deployment), mounting turns into a no-op and
    the JSON API keeps working.
    """
    del settings
    static_dir = STATIC_DIR
    if not static_dir.is_dir():
        return
    if (APP_DIR / "index.html").is_file():
        # The React workbench is built with base "/app/"; serving its assets
        # from that prefix keeps the legacy static page available at /static.
        @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
        def app_index() -> ContentFileResponse:
            return ContentFileResponse(APP_DIR / "index.html")

        app.mount("/app", ContentStaticFiles(directory=APP_DIR, html=True), name="web-app")
        # Keep the build-free page reachable for fallback and for older links.
        app.mount("/static", ContentStaticFiles(directory=static_dir), name="web-static")
        return

    @app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
    def index() -> ContentFileResponse:
        return ContentFileResponse(static_dir / "index.html")

    # The page references /static/*; serve that prefix and then mount the same
    # directory at / for index.html fallbacks. Both registrations happen after
    # the API routes, so /api/* always resolves to the application routes.
    app.mount("/static", ContentStaticFiles(directory=static_dir), name="web-static")
    app.mount("/", ContentStaticFiles(directory=static_dir, html=True), name="web")

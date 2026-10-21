"""Bundled, build-free web UI served next to the read-only product API.

The UI is plain HTML/CSS/JS so it works offline and from an installed wheel.
``mount_web`` is called after ``create_app``; it must never shadow ``/api/*``.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import FileResponse
from starlette.staticfiles import StaticFiles

from .settings import Settings

STATIC_DIR = Path(__file__).with_name("static")
APP_DIR = STATIC_DIR / "app"


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
        @app.get("/", include_in_schema=False)
        def app_index() -> FileResponse:
            return FileResponse(APP_DIR / "index.html")

        app.mount("/app", StaticFiles(directory=APP_DIR, html=True), name="web-app")
        # Keep the build-free page reachable for fallback and for older links.
        app.mount("/static", StaticFiles(directory=static_dir), name="web-static")
        return

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(static_dir / "index.html")

    # The page references /static/*; serve that prefix and then mount the same
    # directory at / for index.html fallbacks. Both registrations happen after
    # the API routes, so /api/* always resolves to the application routes.
    app.mount("/static", StaticFiles(directory=static_dir), name="web-static")
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="web")

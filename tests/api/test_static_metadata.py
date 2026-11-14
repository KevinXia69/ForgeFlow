from __future__ import annotations

import asyncio
import hashlib
import os

import pytest
from fastapi.testclient import TestClient

from forgeflow.api import web
from forgeflow.api.app import create_app
from forgeflow.api.settings import Settings
from forgeflow.api.web import mount_web


def _client(settings: Settings) -> TestClient:
    app = create_app(settings)
    mount_web(app, settings)
    return TestClient(app, base_url="https://testserver")


@pytest.fixture
def content_files(settings, monkeypatch, tmp_path):
    static = tmp_path / "static"
    app_dir = static / "app"
    app_dir.mkdir(parents=True)
    body = b"abcdef0123456789"
    (app_dir / "index.html").write_bytes(body)
    (app_dir / "asset.txt").write_bytes(body)
    (app_dir / "404.html").write_bytes(b"not found")
    monkeypatch.setattr(web, "STATIC_DIR", static)
    monkeypatch.setattr(web, "APP_DIR", app_dir)
    with _client(settings) as client:
        yield client, app_dir / "asset.txt", body


@pytest.mark.parametrize("path", ["/", "/app/", "/app/asset.txt", "/static/app/asset.txt"])
def test_static_content_headers_and_head_do_not_reveal_file_time(content_files, path):
    client, _, body = content_files
    get = client.get(path)
    head = client.head(path)
    expected = '"' + hashlib.sha256(body).hexdigest() + '"'
    for response in (get, head):
        assert response.status_code == 200
        assert "last-modified" not in response.headers
        assert response.headers["etag"] == expected
        assert response.headers["content-length"] == str(len(body))
        assert response.headers["cache-control"] == "no-store"
    assert get.content == body and head.content == b""


def test_static_etag_tracks_bytes_not_deploy_mtime(content_files):
    client, path, body = content_files
    before = client.get("/app/asset.txt").headers["etag"]
    old = path.stat()
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 600_000_000_000))
    assert client.get("/app/asset.txt").headers["etag"] == before
    changed = body[::-1]
    path.write_bytes(changed)
    # Same size and preserved modification time must still invalidate the cache.
    os.utime(path, ns=(old.st_atime_ns, old.st_mtime_ns + 600_000_000_000))
    response = client.get("/app/asset.txt", headers={"If-None-Match": before})
    assert response.status_code == 200 and response.content == changed
    assert response.headers["etag"] == '"' + hashlib.sha256(changed).hexdigest() + '"'
    assert response.headers["etag"] != before


def test_static_conditionals_accept_only_content_etags(content_files):
    client, _, body = content_files
    etag = client.get("/app/asset.txt").headers["etag"]
    for value in (etag, "W/" + etag, '"other", ' + etag, "*"):
        response = client.get("/app/asset.txt", headers={"If-None-Match": value})
        assert response.status_code == 304 and response.content == b""
        assert response.headers["etag"] == etag
        assert "last-modified" not in response.headers
        assert response.headers["cache-control"] == "no-store"
    assert client.head("/app/asset.txt", headers={"If-None-Match": etag}).status_code == 304
    for headers in (
        {"If-Modified-Since": "Fri, 31 Dec 2099 23:59:59 GMT"},
        {"If-None-Match": '"different"', "If-Modified-Since": "Fri, 31 Dec 2099 23:59:59 GMT"},
    ):
        response = client.get("/app/asset.txt", headers=headers)
        assert response.status_code == 200 and response.content == body


def test_static_ranges_use_strong_content_etags_without_date_probes(content_files):
    client, _, body = content_files
    etag = client.get("/app/asset.txt").headers["etag"]
    for value in (None, etag):
        headers = {"Range": "bytes=1-3"}
        if value:
            headers["If-Range"] = value
        response = client.get("/app/asset.txt", headers=headers)
        assert response.status_code == 206 and response.content == body[1:4]
        assert response.headers["content-range"] == f"bytes 1-3/{len(body)}"
        assert response.headers["etag"] == etag and "last-modified" not in response.headers
        head = client.head("/app/asset.txt", headers=headers)
        assert head.status_code == 206 and head.content == b""
        assert head.headers["content-length"] == "3"
    for value in ("W/" + etag, '"different"', "Fri, 31 Dec 2099 23:59:59 GMT"):
        response = client.get("/app/asset.txt", headers={"Range": "bytes=1-3", "If-Range": value})
        assert response.status_code == 200 and response.content == body
    assert client.get("/app/asset.txt", headers={"Range": "bytes=999-"}).status_code == 416


def test_static_hash_runs_off_the_event_loop_and_html_404_is_covered(content_files, monkeypatch):
    client, _, _ = content_files
    original = web._content_etag
    calls = []

    def checked_hash(*args):
        with pytest.raises(RuntimeError, match="no running event loop"):
            asyncio.get_running_loop()
        calls.append(args[0])
        return original(*args)

    monkeypatch.setattr(web, "_content_etag", checked_hash)
    assert client.get("/app/asset.txt").status_code == 200
    missing = client.get("/app/missing")
    assert missing.status_code == 404 and missing.content == b"not found"
    assert "last-modified" not in missing.headers
    assert missing.headers["etag"] == '"' + hashlib.sha256(missing.content).hexdigest() + '"'
    assert client.get("/app/missing", headers={"If-None-Match": missing.headers["etag"]}).status_code == 404
    assert len(calls) >= 3


def test_legacy_and_standalone_workbench_html_hide_file_timestamps(settings, monkeypatch, tmp_path):
    static = tmp_path / "legacy"
    static.mkdir()
    (static / "index.html").write_text("legacy console")
    monkeypatch.setattr(web, "STATIC_DIR", static)
    monkeypatch.setattr(web, "APP_DIR", static / "absent-app")
    app = create_app(settings, review_session_factory=lambda _cases: object())
    with TestClient(app, base_url="https://testserver") as client:
        for path in ("/", "/static/index.html", "/investigation", "/recovery-review"):
            response = client.get(path)
            assert response.status_code == 200, path
            assert "last-modified" not in response.headers
            assert response.headers["etag"] == '"' + hashlib.sha256(response.content).hexdigest() + '"'

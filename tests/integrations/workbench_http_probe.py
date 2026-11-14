"""Real local HTTP acceptance; credentials remain inside the API container."""
from __future__ import annotations

import hashlib
import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request


def local_session():
    """A closed-loopback HTTP client; never follows a cross-origin redirect."""
    base = "http://127.0.0.1:8000"
    jar = http.cookiejar.CookieJar()

    class Redirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, req, fp, code, msg, headers, newurl):
            if urllib.parse.urlsplit(newurl).netloc != "127.0.0.1:8000":
                raise RuntimeError("unexpected_redirect")
            return super().redirect_request(req, fp, code, msg, headers, newurl)

    client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar), Redirect())

    def request(path, *, method="GET", body=None, cookie=None):
        headers = {"Origin": base}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if cookie is not None:
            headers["Cookie"] = cookie
        req = urllib.request.Request(base + path, method=method, headers=headers,
                                     data=json.dumps(body).encode() if body is not None else None)
        try:
            with client.open(req, timeout=60) as response:
                return response.status, response.read(), response.headers
        except urllib.error.HTTPError as error:
            return error.code, error.read(), error.headers

    def read(path):
        status, raw, _ = request(path)
        assert status == 200, "http_read_failed"
        return json.loads(raw)

    return request, read, jar


STAGE = "isolation"


def probe():
    global STAGE
    if os.environ.get("FORGEFLOW_WORKBENCH_ISOLATED_TEST") != "1":
        raise RuntimeError("disposable_workbench_probe_required")
    from forgeflow.local_workbench.config import load_config

    config = load_config()
    base = "http://127.0.0.1:8000"
    request, read, jar = local_session()

    STAGE = "unauthenticated_home"
    assert request("/api/auth/me")[0] == 401
    status, html, _ = request("/app/")
    assert status == 200 and b'id="root"' in html
    assets = re.findall(r'(?:src|href)="([^"<>]+\.(?:js|css))"', html.decode())
    assert assets and any(path.endswith(".js") for path in assets)
    for path in assets:
        resolved = urllib.parse.urljoin(base + "/app/", path)
        assert urllib.parse.urlsplit(resolved).netloc == "127.0.0.1:8000"
        status, content, _ = request(urllib.parse.urlsplit(resolved).path)
        assert status == 200 and content, "frontend_asset_missing"
    assert read("/api/health") == {"ok": True}
    assert read("/api/config")["deployment"] == "local"
    STAGE = "login"
    login = {"invite_code": config["env"]["FORGEFLOW_INVITE_CODES"]}
    assert request("/api/auth/session", method="POST", body=login)[0] == 201
    saved_cookie = "; ".join(f"{cookie.name}={cookie.value}" for cookie in jar)
    assert saved_cookie
    checks = {"unauthenticated_rejected": True, "home_and_assets": True, "login": True}
    try:
        assert read("/api/auth/me").get("scope_id")
        binding = read("/api/odoo20/binding")
        assert binding == {"bound": True, "role": "admin"}
        STAGE = "reference_catalog"
        documents = read("/api/knowledge/documents")
        assert len(documents) == 12
        document_count = 0
        block_count = 0
        versions = []
        for document in documents:
            assert document["parse_status"] == "complete"
            assert document["lifecycle"]["searchable"] is True
            assert document["lifecycle"]["withdrawn"] is False
            path = urllib.parse.quote(document["source_path"], safe="/")
            version = document["version"]
            blocks = read(f"/api/knowledge/documents/{path}/blocks?version={version}")
            assert blocks["version"] == version and blocks["blocks"]
            assert read(f"/api/knowledge/documents/{path}/versions")
            block_count += len(blocks["blocks"])
            document_count += 1
            versions.append(version)
        STAGE = "erp_reads"
        counts = {}
        for kind in ("purchase_order", "sale_order", "bom", "stock"):
            rows = read(f"/api/odoo20/business-data?kind={kind}&limit=50")
            assert rows["kind"] == kind and rows["count"] == len(rows["records"]) > 0
            assert rows["truncated"] is False
            counts[kind] = rows["count"]
            if kind == "purchase_order":
                reference = urllib.parse.quote(rows["records"][0]["name"], safe="")
                resolved = read("/api/odoo20/resolve?reference=" + reference)
                assert resolved["status"] == "resolved"
        assert isinstance(read("/api/odoo20/tasks"), list)
        checks.update(reference_catalog=True, current_version_blocks=True,
                      bound_erp_lookup=True, literal_reference_resolution=True)
        digest = hashlib.sha256(json.dumps(documents, sort_keys=True).encode()).hexdigest()
    finally:
        assert request("/api/auth/session", method="DELETE")[0] == 204
        assert request("/api/auth/me")[0] == 401
        assert request("/api/auth/me", cookie=saved_cookie)[0] == 401
    checks["logout_and_old_cookie_rejected"] = True
    return {"ok": True, "checks": checks, "documents": document_count,
            "document_versions": sorted(versions), "blocks": block_count,
            "catalog_sha256": digest, "erp_counts": counts,
            "real_provider_requests": 0, "erp_mutation_requests": 0}


if __name__ == "__main__":
    try:
        result = probe()
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__, "stage": STAGE}
    print("FORGEFLOW_WORKBENCH_HTTP_PROBE=" + json.dumps(result), flush=True)
    raise SystemExit(0 if result["ok"] else 1)

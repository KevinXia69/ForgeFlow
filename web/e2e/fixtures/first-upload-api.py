"""Real API/PostgreSQL fixture; only external semantic HTTP services are simulated."""
from __future__ import annotations

import json
import os
import ssl
import subprocess
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import uvicorn
import httpx

from forgeflow.api.auth import issue_cookie, verify_cookie
from forgeflow.api.settings import create_settings
from forgeflow.knowledge.catalog import KnowledgeCatalog
from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.semantic.config import VECTOR_SIZE
from forgeflow.superagent.bindings import bind_odoo_user
from forgeflow.superagent.store import ControlStore


def main() -> None:
    database_env = Path(os.environ["FIRST_UPLOAD_DATABASE_ENV"])
    if database_env.stat().st_mode & 0o077:
        raise RuntimeError("database_env_must_be_private")
    values = dict(line.removeprefix("export ").split("=", 1)
                  for line in database_env.read_text().splitlines()
                  if line.strip() and not line.startswith("#"))
    database_url = values["FORGEFLOW_CONTROL_DATABASE_URL"].strip().strip("'\"")
    output = Path(os.environ["FIRST_UPLOAD_OUTPUT"])
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    port = int(os.environ.get("FIRST_UPLOAD_PORT", "4198"))
    provider_port = int(os.environ.get("FIRST_UPLOAD_PROVIDER_PORT", "4199"))
    tenant = "ui-first-" + uuid.uuid4().hex
    provider_calls: list[str] = []
    points: dict = {}

    def matches(point: dict, query: dict) -> bool:
        return all(point["payload"].get(item["key"]) == item["match"]["value"]
                   for item in query.get("must", []))

    class Provider(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            return

        def handle_request(self):
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
            provider_calls.append(f"{self.command} {path}")
            if path == "/embeddings":
                result = {"data": [{"embedding": [0.01] * VECTOR_SIZE} for _ in body["input"]]}
            elif path == "/rerank":
                result = {"results": [{"index": i, "relevance_score": 0.9} for i in range(len(body["documents"]))]}
            elif path.endswith("/points") and self.command == "PUT":
                points.update({point["id"]: point for point in body["points"]})
                result = {"result": {"status": "completed"}}
            elif path.endswith("/points/payload"):
                for point in points.values():
                    if matches(point, body["filter"]):
                        point["payload"].update(body["payload"])
                result = {"result": {"status": "completed"}}
            elif path.endswith("/points/search"):
                result = {"result": [{**point, "score": 0.9} for point in points.values() if matches(point, body["filter"])]}
            else:
                result = {"result": {}}
            raw = json.dumps(result).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        do_GET = handle_request
        do_POST = handle_request
        do_PUT = handle_request

    cert, key = output / "provider-cert.pem", output / "provider-key.pem"
    subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
        "-keyout", str(key), "-out", str(cert), "-days", "1", "-subj", "/CN=127.0.0.1",
        "-addext", "subjectAltName=IP:127.0.0.1"], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    key.chmod(0o600)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(str(cert), str(key))
    provider = ThreadingHTTPServer(("127.0.0.1", provider_port), Provider)
    provider.socket = tls.wrap_socket(provider.socket, server_side=True)
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    # Keep real HTTP transport and certificate verification, trusting this fixture CA.
    client = httpx.Client
    trust = ssl.create_default_context(cafile=str(cert))
    httpx.Client = lambda **kwargs: client(**{"verify": trust, **kwargs})
    os.environ.update({
        "FORGEFLOW_CONTROL_DATABASE_URL": database_url,
        "FORGEFLOW_CONTROL_TENANT_ID": tenant,
        "FORGEFLOW_KNOWLEDGE_TENANT": tenant,
        "FORGEFLOW_DOCMIND_MODE": "off",
        "FORGEFLOW_DSH_COMMAND": "",
        "QDRANT_URL": f"https://127.0.0.1:{provider_port}",
        "QDRANT_API_KEY": "local-fixture",
        "SEMANTIC_API_KEY": "local-fixture",
        "EMBEDDING_API_URL": f"https://127.0.0.1:{provider_port}/embeddings",
        "EMBEDDING_MODEL_ID": "local-fixture",
        "RERANKER_API_URL": f"https://127.0.0.1:{provider_port}/rerank",
        "RERANKER_MODEL_ID": "local-fixture",
    })
    invite = uuid.uuid4().hex
    (output / "runs").mkdir(exist_ok=True)
    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(output / "runs"),
        "FORGEFLOW_SESSION_SECRET": uuid.uuid4().hex,
        "FORGEFLOW_INVITE_CODES": invite,
        "FORGEFLOW_SECURE_COOKIE": "false",
        "FORGEFLOW_DEPLOYMENT": "local",
    })
    audience = f"127.0.0.1:{port}"
    cookie = issue_cookie(settings, invite, audience=audience)
    principal = verify_cookie(settings, cookie, audience=audience)
    assert principal is not None
    store = ControlStore(database_url)
    store.migrate()
    store.seed_tenant(tenant, "First upload browser acceptance")
    bind_odoo_user(store, principal_id=principal, tenant_id=tenant, odoo_user_id=1, role="admin")
    assert KnowledgeCatalog(store).published(tenant) is None
    assert DocumentCatalog(store).list_documents(tenant) == []
    store.close()
    cookie_file = output / "cookie.json"
    cookie_file.write_text(json.dumps([{"name": settings.cookie_name, "value": cookie,
        "domain": "127.0.0.1", "path": "/", "httpOnly": True, "secure": False, "sameSite": "Lax"}]))
    cookie_file.chmod(0o600)
    import forgeflow.api.web as web
    web.STATIC_DIR = Path(os.environ["FIRST_UPLOAD_STATIC_DIR"])
    web.APP_DIR = web.STATIC_DIR / "app"
    from forgeflow.api.app import create_app
    app = create_app(settings)

    @app.get("/fixture/report")
    def report():
        fresh = ControlStore(database_url)
        try:
            return {"tenant": tenant, "published_generation": KnowledgeCatalog(fresh).published(tenant),
                    "documents": DocumentCatalog(fresh).list_documents(tenant),
                    "provider_calls": provider_calls,
                    "indexed_points": [{"path": p["payload"]["path"], "generation": p["payload"].get("generation"),
                        "status": p["payload"].get("status")} for p in points.values()]}
        finally:
            fresh.close()

    try:
        uvicorn.run(app, host="127.0.0.1", port=port, access_log=False, log_level="warning")
    finally:
        provider.shutdown()
        cleanup = ControlStore(database_url)
        try:
            with cleanup.transaction() as cursor:
                cursor.execute("DELETE FROM control_odoo_bindings WHERE principal_id=%s", (principal,))
                cursor.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        finally:
            cleanup.close()
        cookie_file.unlink(missing_ok=True)
        key.unlink(missing_ok=True)


if __name__ == "__main__":
    main()

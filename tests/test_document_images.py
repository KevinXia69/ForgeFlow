"""Document Mind images: bounded download, tenant storage and image ACL."""
from __future__ import annotations

import json
import os
import uuid

import pytest

from forgeflow.knowledge.docmind import layouts_to_blocks
from forgeflow.knowledge.images import DocumentImageStore, ImageStoreError
from forgeflow.superagent import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")

URL = ("https://bucket.oss-cn-hangzhou.aliyuncs.com/doc/image.png"
       "?Expires=1&OSSAccessKeyId=k&Signature=secret")


def _tenant(store) -> str:
    tenant = f"images-{uuid.uuid4().hex}"
    store.migrate()
    store.seed_tenant(tenant, "Images", 1, (("planner", "Planner", "planner"),))
    return tenant


def _fetcher(content_type: str = "image/png", payload: bytes = b"\x89PNG-test"):
    calls: list[str] = []

    def fetch(url, *, max_bytes):
        calls.append(url)
        return content_type, payload

    fetch.calls = calls  # type: ignore[attr-defined]
    return fetch


def test_an_image_is_stored_privately_and_read_back_with_its_acl(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    other = _tenant(store)
    fetch = _fetcher()
    images = DocumentImageStore(store, tmp_path / "images", fetch=fetch)
    try:
        stored = images.persist(URL, tenant_id=tenant, document_id="d1",
                                source_sha256="a" * 64, acl_scope="project")
        path = tmp_path / "images" / stored.storage_path
        assert path.is_file() and path.stat().st_mode & 0o077 == 0
        again = images.persist(URL, tenant_id=tenant, document_id="d1")
        assert again.sha256 == stored.sha256 and len(fetch.calls) == 2
        assert images.read(tenant_id=tenant, sha256=stored.sha256,
                           acl_scopes={"project"}) is not None
        assert images.read(tenant_id=tenant, sha256=stored.sha256,
                           acl_scopes={"engineering"}) is None
        assert images.read(tenant_id=other, sha256=stored.sha256,
                           acl_scopes={"project"}) is None
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_tenants WHERE id=ANY(%s)", ([tenant, other],))
        store.close()


def test_foreign_or_plain_urls_are_refused_before_any_fetch(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    fetch = _fetcher()
    images = DocumentImageStore(store, tmp_path / "images", fetch=fetch)
    try:
        for url, code in (
            ("http://bucket.oss-cn-hangzhou.aliyuncs.com/x.png", "image_url_scheme_rejected"),
            ("https://evil.example/x.png", "image_url_host_rejected"),
            ("https://user@bucket.aliyuncs.com/x.png", "image_url_host_rejected"),
            ("https://bucket.aliyuncs.com:8443/x.png", "image_url_port_rejected"),
        ):
            with pytest.raises(ImageStoreError) as error:
                images.persist(url, tenant_id=tenant, document_id="d1")
            assert error.value.code == code
        assert fetch.calls == []
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_oversize_and_non_image_payloads_are_rejected(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        too_large = DocumentImageStore(
            store, tmp_path / "images", max_bytes=4,
            fetch=lambda url, *, max_bytes: ("image/png", b"12345"))
        with pytest.raises(ImageStoreError) as large:
            too_large.persist(URL, tenant_id=tenant, document_id="d1")
        assert large.value.code == "image_too_large"
        html = DocumentImageStore(
            store, tmp_path / "images2", fetch=lambda url, *, max_bytes: (
                "text/html", b"<script>"))
        with pytest.raises(ImageStoreError) as kind:
            html.persist(URL, tenant_id=tenant, document_id="d1")
        assert kind.value.code == "image_content_type_rejected"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_blocks_keep_the_hash_and_never_the_signed_url(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    fetch = _fetcher()
    images = DocumentImageStore(store, tmp_path / "images", fetch=fetch)
    try:
        blocks = layouts_to_blocks(
            {"layouts": [{"type": "text", "pageNum": 0,
                          "markdownContent": f"![](<{URL}>)"}]},
            document_id="d1", version=1, source_sha256="a" * 64,
            image_store=images, image_tenant=tenant, acl_scope="project")
        payload = json.dumps(blocks)
        assert "Signature=" not in payload and "OSSAccessKeyId" not in payload
        assert "image:" in payload
        assert blocks[0]["images"][0]["content_type"] == "image/png"
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_deleting_a_document_removes_its_image_rows_and_files(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    images = DocumentImageStore(store, tmp_path / "images", fetch=_fetcher())
    try:
        stored = images.persist(URL, tenant_id=tenant, document_id="d1")
        path = tmp_path / "images" / stored.storage_path
        assert images.delete_for_document(tenant_id=tenant, document_id="d1") == 1
        assert not path.exists()
        assert images.read(tenant_id=tenant, sha256=stored.sha256,
                           acl_scopes={"project"}) is None
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_the_same_content_can_have_one_link_per_acl(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    images = DocumentImageStore(store, tmp_path / "images", fetch=_fetcher())
    try:
        project = images.persist(URL, tenant_id=tenant, document_id="d1",
                                 acl_scope="project")
        engineering = images.persist(URL, tenant_id=tenant, document_id="d2",
                                      acl_scope="engineering")
        assert project.sha256 == engineering.sha256
        assert images.read(tenant_id=tenant, sha256=project.sha256,
                           acl_scopes={"project"}) is not None
        assert images.read(tenant_id=tenant, sha256=project.sha256,
                           acl_scopes={"engineering"}) is not None
        assert images.read(tenant_id=tenant, sha256=project.sha256,
                           acl_scopes={"other"}) is None
        assert images.delete_for_document(tenant_id=tenant, document_id="d1") == 1
        assert (tmp_path / "images" / project.storage_path).is_file()
        assert images.delete_for_document(tenant_id=tenant, document_id="d2") == 1
        assert not (tmp_path / "images" / project.storage_path).exists()
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_the_parse_worker_persists_images_and_strips_the_url(tmp_path) -> None:
    from forgeflow.knowledge.docmind_jobs import ParseJobStore, ParseWorker

    class Client:
        def submit(self, path, filename, extension, **kwargs):
            return "vendor-1"

        def status(self, job_id):
            return {"status": "success"}

        def result(self, job_id):
            return {"layouts": [{
                "type": "text", "pageNum": 0,
                "markdownContent": f"See ![]({URL})"}]}

    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    source = tmp_path / "scan.pdf"
    source.write_bytes(b"%PDF-1.4 scan")
    images = DocumentImageStore(store, tmp_path / "images", fetch=_fetcher())
    jobs = ParseJobStore(store)
    job = jobs.enqueue(tenant_id=tenant, document_id="doc-img", document_version=1,
                       source_path=str(source), source_sha256="c" * 64,
                       conversion={"filename": "scan.pdf", "extension": "pdf",
                                   "original_path": str(source),
                                   "acl_scope": "project"})
    try:
        worker = ParseWorker(jobs, Client(), poll_seconds=0, images=images)
        worker.tick(tenant_id=tenant)
        worker.tick(tenant_id=tenant)
        row = jobs.get(tenant_id=tenant, job_id=job["id"])
        assert row["state"] == "succeeded"
        assert "Signature=" not in row["blocks_json"]
        assert "image:" in row["blocks_json"]
        stored = store.connection.execute(
            "SELECT sha256, acl_scope FROM knowledge_document_images "
            "WHERE tenant_id=%s", (tenant,)).fetchone()
        store.connection.commit()
        assert stored is not None and stored["acl_scope"] == "project"
        assert (tmp_path / "images" / tenant / stored["sha256"][:2]).is_dir()
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_the_image_api_enforces_tenant_and_role_acl(tmp_path, monkeypatch) -> None:
    from fastapi.testclient import TestClient

    from forgeflow.api.app import create_app
    from forgeflow.api.settings import create_settings
    from forgeflow.superagent import bind_odoo_user

    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    images = DocumentImageStore(store, tmp_path / "images", fetch=_fetcher())
    invite = f"image-invite-{uuid.uuid4().hex}"
    monkeypatch.setenv("FORGEFLOW_DOCUMENT_IMAGE_ROOT", str(tmp_path / "images"))
    monkeypatch.setenv("FORGEFLOW_CONTROL_DATABASE_URL", DATABASE_URL)
    try:
        project = images.persist(URL, tenant_id=tenant, document_id="d1",
                                 acl_scope="project")
        engineering = DocumentImageStore(
            store, tmp_path / "images",
            fetch=lambda url, *, max_bytes: ("image/png", b"engineering-only")).persist(
            "https://bucket.aliyuncs.com/secret.png?Signature=x", tenant_id=tenant,
            document_id="d2", acl_scope="engineering")
        settings = create_settings({
            "FORGEFLOW_DATA_ROOT": str(tmp_path / "runs"),
            "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "state"),
            "FORGEFLOW_SESSION_SECRET": "image-api-session-secret-0123456789",
            "FORGEFLOW_INVITE_CODES": invite,
            "FORGEFLOW_SECURE_COOKIE": "false",
        })
        client = TestClient(create_app(settings))
        with client:
            session = client.post("/api/auth/session", json={"invite_code": invite})
            uid = session.json()["uid"]
            bind_odoo_user(store, principal_id=uid, tenant_id=tenant,
                           odoo_user_id=2, role="observer")
            ok = client.get(f"/api/knowledge/images/{project.sha256}")
            assert ok.status_code == 200 and ok.headers["content-type"].startswith(
                "image/png")
            assert ok.headers["x-content-type-options"] == "nosniff"
            hidden = client.get(f"/api/knowledge/images/{engineering.sha256}")
            assert hidden.status_code == 404
            assert client.get("/api/knowledge/images/" + "0" * 64).status_code == 404
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_odoo_bindings WHERE tenant_id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_deleting_by_source_hash_clears_old_image_links(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    fetch = _fetcher()
    images = DocumentImageStore(store, tmp_path / "images", fetch=fetch)
    try:
        source = "e" * 64
        first = images.persist(URL, tenant_id=tenant, document_id="doc-a",
                               source_sha256=source, acl_scope="project")
        second = images.persist(
            "https://bucket.aliyuncs.com/other.png?Signature=y", tenant_id=tenant,
            document_id="doc-b", source_sha256=source, acl_scope="project")
        assert images.read(tenant_id=tenant, sha256=first.sha256,
                           acl_scopes={"project"}) is not None
        assert images.delete_for_source(tenant_id=tenant,
                                        source_sha256=source) == 2
        assert images.read(tenant_id=tenant, sha256=first.sha256,
                           acl_scopes={"project"}) is None
        assert not (tmp_path / "images" / second.storage_path).exists()
    finally:
        with store.connection.transaction():
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()

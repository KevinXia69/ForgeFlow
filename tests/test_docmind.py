"""Document Mind config, layout conversion and the durable job worker."""
from __future__ import annotations

import os
import uuid
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from forgeflow.knowledge.docmind import (
    DocMindConfig,
    DocumentMindClient,
    layouts_to_blocks,
)
from forgeflow.knowledge.docmind_jobs import ParseJobStore, ParseWorker
from forgeflow.superagent import ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
requires_db = pytest.mark.skipif(not DATABASE_URL,
                                 reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_config_loads_only_from_the_standard_environment_chain() -> None:
    assert DocMindConfig.from_env({}) is None
    assert DocMindConfig.missing({}) == ["ALIBABA_CLOUD_ACCESS_KEY_ID",
                                        "ALIBABA_CLOUD_ACCESS_KEY_SECRET"]
    config = DocMindConfig.from_env({
        "ALIBABA_CLOUD_ACCESS_KEY_ID": "test-id",
        "ALIBABA_CLOUD_ACCESS_KEY_SECRET": "test-secret",
        "FORGEFLOW_DOCMIND_REGION": "cn-shanghai"})
    assert config is not None
    assert config.region == "cn-shanghai"
    assert config.as_public() == {"endpoint": "docmind-api.cn-hangzhou.aliyuncs.com",
                                  "region": "cn-shanghai", "mode": "acs",
                                  "credential": "access_key", "workspace": ""}
    assert "test-secret" not in str(config.as_public())


def test_a_bailian_sk_key_selects_the_dashscope_path() -> None:
    config = DocMindConfig.from_env({
        "DASHSCOPE_API_KEY": "sk-test-not-a-real-key",
        "DASHSCOPE_WORKSPACE_ID": "ws-test"})
    assert config is not None
    assert config.mode == "bailian"
    assert config.endpoint == "https://dashscope.aliyuncs.com"
    assert config.as_public()["credential"] == "bailian_api_key"
    assert "sk-test" not in str(config.as_public())


def test_layouts_keep_page_locator_and_only_real_positions() -> None:
    blocks = layouts_to_blocks({
        "layouts": [
            {"type": "title", "level": 1, "text": "Contract", "pageNum": 0},
            {"type": "text", "markdownContent": "Amount 100", "pageNum": 2,
             "pos": {"x": 1, "y": 2}, "layoutConf": 0.97},
            {"type": "table", "text": "|a|b|", "pageNum": 2, "pos": {"x": 3}},
            {"type": "text", "text": "no page"},
        ]}, document_id="doc-1", version=3, source_sha256="a" * 64)
    assert [block["block_type"] for block in blocks] == [
        "section", "paragraph", "table", "paragraph"]
    assert blocks[1]["locator"] == "page:3#1"
    assert blocks[1]["pos"] == {"x": 1, "y": 2}
    assert blocks[1]["confidence"] == 0.97
    assert blocks[2]["pos"] == {"x": 3}
    assert blocks[3]["page"] is None
    assert blocks[3]["locator"] == "doc:doc-1@v3#3"
    assert blocks[3]["pos"] is None


class _FakeSdk:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def query_doc_parser_status(self, request, runtime):
        return SimpleNamespace(body=SimpleNamespace(
            data=SimpleNamespace(status="success", page_count_estimate=3,
                                 paragraph_count=2, table_count=1, image_count=0,
                                 tokens=10, processing=1.0),
            message=""))

    def get_doc_parser_result(self, request, runtime):
        self.calls.append(request.layout_num)
        page = self.pages[len(self.calls) - 1]
        return SimpleNamespace(body=SimpleNamespace(data={"layouts": page}))


def test_result_pagination_merges_every_layout() -> None:
    sdk = _FakeSdk([list(range(100)), [100]])
    config = DocMindConfig("docmind-api.cn-hangzhou.aliyuncs.com", "cn-hangzhou",
                           "id", "secret")
    client = DocumentMindClient(config, client=sdk)
    result = client.result("job-1", page_size=100)
    assert len(result["layouts"]) == 101
    assert sdk.calls == [0, 100]
    status = client.status("job-1")
    assert status["status"] == "success" and status["page_count"] == 3


class _FakeClient:
    def __init__(self):
        self.submitted = []
        self.polled = 0

    def submit(self, path, filename, extension, **kwargs):
        self.submitted.append((path, filename, extension))
        return "vendor-1"

    def status(self, job_id):
        self.polled += 1
        return {"status": "success"}

    def result(self, job_id):
        return {"layouts": [{"type": "text", "text": "parsed by document mind",
                             "pageNum": 0}]}


def _tenant(store):
    tenant = f"docmind-{uuid.uuid4().hex}"
    store.migrate()
    store.seed_tenant(tenant, "DocMind", 1, (("planner", "Planner", "planner"),))
    return tenant


@requires_db
def test_jobs_are_deduplicated_and_the_worker_parses_once(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        source = tmp_path / "contract.pdf"
        source.write_bytes(b"%PDF-1.4 test")
        jobs = ParseJobStore(store)
        first = jobs.enqueue(tenant_id=tenant, document_id="doc-1", document_version=1,
                             source_path=str(source), source_sha256="b" * 64)
        second = jobs.enqueue(tenant_id=tenant, document_id="doc-1", document_version=1,
                              source_path=str(source), source_sha256="b" * 64)
        assert first["id"] == second["id"]

        client = _FakeClient()
        worker = ParseWorker(jobs, client, poll_seconds=0)
        assert worker.tick(tenant_id=tenant)["processed"] == 1
        assert client.submitted and client.submitted[0][2] == "pdf"
        result = worker.tick(tenant_id=tenant)
        assert result["processed"] == 1
        row = jobs.get(tenant_id=tenant, job_id=first["id"])
        assert row["state"] == "succeeded"
        assert "parsed by document mind" in row["blocks_json"]
        usage = store.connection.execute(
            "SELECT vendor_calls, pages, blocks FROM knowledge_docmind_usage "
            "WHERE tenant_id=%s", (tenant,)).fetchone()
        store.connection.commit()
        assert usage is not None and usage["vendor_calls"] == 1
        assert usage["pages"] >= 1 and usage["blocks"] >= 1
        other = jobs.enqueue(tenant_id=tenant + "-other", document_id="doc-2",
                             document_version=1, source_path=str(source),
                             source_sha256="b" * 64)
        assert other["id"] != first["id"]
        assert jobs.get(tenant_id=tenant + "-other", job_id=first["id"]) is None
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM knowledge_parse_jobs WHERE tenant_id IN (%s, %s)",
                (tenant, tenant + "-other"))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s",
                                     (tenant + "-other",))
        store.close()


@requires_db
def test_a_failed_submission_is_retried_with_a_bound(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        source = tmp_path / "scan.pdf"
        source.write_bytes(b"scan")
        jobs = ParseJobStore(store)
        job = jobs.enqueue(tenant_id=tenant, document_id="doc-2",
                           document_version=1, source_path=str(source),
                           source_sha256="c" * 64)

        class Failing:
            def submit(self, *args, **kwargs):
                from forgeflow.knowledge.docmind import DocMindError
                raise DocMindError("docmind_unavailable")

        worker = ParseWorker(jobs, Failing(), max_attempts=1)
        result = worker.tick(tenant_id=tenant)
        assert result["results"][0]["state"] == "failed"
        row = jobs.get(tenant_id=tenant, job_id=job["id"])
        assert row["state"] == "failed" and row["error_code"] == "docmind_unavailable"
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM knowledge_parse_jobs WHERE tenant_id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


@requires_db
def test_worker_result_is_written_to_the_document_catalogue(tmp_path) -> None:
    from forgeflow.knowledge.docmind_intake import write_result

    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        original = tmp_path / "contract.pdf"
        original.write_bytes(b"%PDF-1.4 original")
        jobs = ParseJobStore(store)
        jobs.enqueue(
            tenant_id=tenant, document_id="doc-3", document_version=1,
            source_path="contract.pdf", source_sha256="d" * 64,
            conversion={"filename": "contract.pdf", "extension": "pdf",
                        "original_path": str(original)})
        written = {}

        def on_complete(row, result, blocks):
            written.update(write_result(store, job=row, result=result,
                                        acl_scope="project"))

        worker = ParseWorker(jobs, _FakeClient(), poll_seconds=0,
                             on_complete=on_complete)
        worker.tick(tenant_id=tenant)
        result = worker.tick(tenant_id=tenant)
        assert result["results"][0]["state"] == "succeeded"
        assert written["blocks"] >= 1
        row = store.connection.execute(
            "SELECT source_path, parse_status FROM knowledge_documents "
            "WHERE tenant_id=%s AND source_path='contract.pdf'", (tenant,)).fetchone()
        assert row is not None and row["parse_status"] == "complete"
        block = store.connection.execute(
            "SELECT locator, page, text FROM knowledge_document_blocks "
            "WHERE tenant_id=%s AND document_id=%s ORDER BY ord LIMIT 1",
            (tenant, written["document_id"])).fetchone()
        assert block["locator"].startswith("page:1#")
        assert "document mind" in block["text"]
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM knowledge_parse_jobs WHERE tenant_id=%s", (tenant,))
            store.connection.execute(
                "DELETE FROM knowledge_documents WHERE tenant_id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


def test_signed_vendor_links_are_not_persisted_in_blocks() -> None:
    signed = ("![logo](http://docmind-api-cn-beijing.oss-cn-beijing.aliyuncs.com/x.png"
              "?Expires=1&OSSAccessKeyId=STS.x&Signature=abc&security-token=secret)")
    blocks = layouts_to_blocks({"layouts": [
        {"type": "text", "text": signed, "pageNum": [0]},
        {"type": "text", "text": "Penalty 1200", "pageNum": [0]},
    ]}, document_id="doc-1", version=1, source_sha256="e" * 64)
    assert blocks[0]["text"] == "[image]"
    assert "security-token" not in blocks[0]["text"]
    assert blocks[1]["text"] == "Penalty 1200"


@requires_db
def test_failed_jobs_are_listed_and_requeued_for_the_setup_panel(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        source = tmp_path / "broken.pdf"
        source.write_bytes(b"%PDF-1.4 broken")
        jobs = ParseJobStore(store)
        job = jobs.enqueue(tenant_id=tenant, document_id="doc-broken",
                           document_version=1, source_path=str(source),
                           source_sha256="f" * 64)
        jobs.fail(job["id"], "docmind_parse_failed")
        listed = jobs.list_jobs(tenant_id=tenant)
        assert listed and listed[0]["id"] == job["id"]
        assert listed[0]["error_code"] == "docmind_parse_failed"
        requeued = jobs.requeue(tenant_id=tenant, job_id=job["id"])
        assert requeued is not None and requeued["state"] == "queued"
        assert requeued["attempts"] == 0
        assert jobs.requeue(tenant_id=tenant, job_id=job["id"]) is None
        assert jobs.list_jobs(tenant_id=tenant + "-other") == []
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM knowledge_parse_jobs WHERE tenant_id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()


@requires_db
def test_requeue_starts_a_fresh_timeout_window(tmp_path) -> None:
    store = ControlStore(DATABASE_URL)
    tenant = _tenant(store)
    try:
        source = tmp_path / "stale.pdf"
        source.write_bytes(b"stale")
        jobs = ParseJobStore(store)
        job = jobs.enqueue(tenant_id=tenant, document_id="stale-doc",
                           document_version=1, source_path=str(source),
                           source_sha256="d" * 64)
        # Simulate a failed job whose submitted_at is already beyond the 30m
        # timeout; the old worker would fail it again before submitting.
        with store.connection.transaction():
            store.connection.execute(
                "UPDATE knowledge_parse_jobs SET state='failed', submitted_at=now() "
                "- interval '2 hours', error_code='docmind_timeout' WHERE id=%s",
                (job["id"],))
        requeued = jobs.requeue(tenant_id=tenant, job_id=job["id"])
        assert requeued is not None and requeued["state"] == "queued"
        row = jobs.get(tenant_id=tenant, job_id=job["id"])
        assert row["submitted_at"] is not None
        assert (datetime.now(UTC) - row["submitted_at"]).total_seconds() < 60
        client = _FakeClient()
        worker = ParseWorker(jobs, client, poll_seconds=0)
        assert worker.tick(tenant_id=tenant)["processed"] == 1
        assert client.submitted
        worker.tick(tenant_id=tenant)
        assert jobs.get(tenant_id=tenant, job_id=job["id"])["state"] == "succeeded"
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM knowledge_parse_jobs WHERE tenant_id=%s", (tenant,))
            store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant,))
        store.close()

"""Durable Document Mind parse jobs and the background poll worker.

The upload request only enqueues; a background worker submits, polls and stores
the result.  Jobs live in PostgreSQL so an API or worker restart resumes them.
The partial unique index on ``(tenant_id, source_sha256)`` stops the same content
being submitted again while a job is active, which prevents uncontrolled duplicate
vendor charges.
"""
from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from ..superagent.store import ControlStore
from .docmind import DocMindError, layouts_to_blocks
from .docmind_costs import DocMindUsageStore
from .images import DocumentImageStore

ACTIVE_STATES = ("queued", "processing")
TERMINAL_STATES = ("succeeded", "failed", "unsupported", "expired")


class JobError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ParseClient(Protocol):
    def submit(self, path: str, filename: str, extension: str, *, llm_enhancement: bool = True,
               output_html_table: bool = True) -> str: ...
    def status(self, job_id: str) -> dict[str, Any]: ...
    def result(self, job_id: str) -> dict[str, Any]: ...


def _now() -> datetime:
    return datetime.now(UTC)


class ParseJobStore:
    def __init__(self, store: ControlStore) -> None:
        self.store = store

    def enqueue(self, *, tenant_id: str, document_id: str, document_version: int,
                source_path: str, source_sha256: str,
                conversion: dict[str, Any] | None = None) -> dict[str, Any]:
        job_id = uuid.uuid4().hex
        with self.store.transaction() as cursor:
            cursor.execute(
                "INSERT INTO knowledge_parse_jobs(id, tenant_id, document_id, "
                "document_version, source_path, source_sha256, conversion_json, state, "
                "next_poll_at) VALUES (%s,%s,%s,%s,%s,%s,%s,'queued',now()) "
                "ON CONFLICT (tenant_id, source_sha256) WHERE state IN "
                "('queued','processing') DO NOTHING RETURNING id",
                (job_id, tenant_id, document_id, int(document_version), source_path,
                 source_sha256,
                 json.dumps(conversion or {}, sort_keys=True)))
            row = cursor.fetchone()
            if row is None:
                row = cursor.execute(
                    "SELECT * FROM knowledge_parse_jobs WHERE tenant_id=%s "
                    "AND source_sha256=%s AND state IN ('queued','processing') "
                    "ORDER BY created_at LIMIT 1",
                    (tenant_id, source_sha256)).fetchone()
                if row is None:
                    raise JobError("job_conflict")
                return dict(row)
            return dict(cursor.execute(
                "SELECT * FROM knowledge_parse_jobs WHERE id=%s", (row["id"],)
            ).fetchone())

    def get(self, *, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        row = self.store.connection.execute(
            "SELECT * FROM knowledge_parse_jobs WHERE tenant_id=%s AND id=%s",
            (tenant_id, job_id)).fetchone()
        self.store.connection.commit()
        return dict(row) if row else None

    def list_jobs(self, *, tenant_id: str, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.store.connection.execute(
            "SELECT id, source_path, state, attempts, error_code, blocks_json, "
            "created_at, updated_at, completed_at FROM knowledge_parse_jobs "
            "WHERE tenant_id=%s ORDER BY created_at DESC LIMIT %s",
            (tenant_id, max(1, min(int(limit), 100)))).fetchall()
        self.store.connection.commit()
        return [dict(row) for row in rows]

    def requeue(self, *, tenant_id: str, job_id: str) -> dict[str, Any] | None:
        """Put a failed job back in the queue; parsing is safe to repeat."""
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "UPDATE knowledge_parse_jobs SET state='queued', attempts=0, "
                "vendor_job_id=NULL, submitted_at=now(), completed_at=NULL, "
                "next_poll_at=now(), error_code=NULL, "
                "updated_at=now() WHERE tenant_id=%s AND id=%s "
                "AND state IN ('failed','unsupported','expired') RETURNING id",
                (tenant_id, job_id)).fetchone()
            if row is None:
                return None
            return dict(cursor.execute(
                "SELECT id, source_path, state, attempts, error_code "
                "FROM knowledge_parse_jobs WHERE id=%s", (job_id,)).fetchone())

    def claim(self, *, limit: int = 5, lease_seconds: int = 120,
              tenant_id: str | None = None) -> list[dict[str, Any]]:
        with self.store.transaction() as cursor:
            clause = ("SELECT * FROM knowledge_parse_jobs WHERE "
                      "state IN ('queued','processing') "
                      "AND (next_poll_at IS NULL OR next_poll_at <= now()) ")
            params: list[Any] = []
            if tenant_id:
                clause += "AND tenant_id=%s "
                params.append(tenant_id)
            clause += "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s"
            params.append(max(1, min(int(limit), 50)))
            rows = cursor.execute(clause, tuple(params)).fetchall()
            if rows:
                cursor.execute(
                    "UPDATE knowledge_parse_jobs SET next_poll_at=now()+ %s * interval '1 second', "
                    "updated_at=now() WHERE id = ANY(%s)",
                    (max(10, int(lease_seconds)), [row["id"] for row in rows]))
            return [dict(row) for row in rows]

    def submitted(self, job_id: str, vendor_job_id: str, *,
                  seconds: int = 10) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_parse_jobs SET state='processing', vendor_job_id=%s, "
                "attempts=attempts+1, submitted_at=now(), "
                "next_poll_at=now()+ %s * interval '1 second', updated_at=now() WHERE id=%s",
                (vendor_job_id, max(0, int(seconds)), job_id))

    def poll_later(self, job_id: str, *, seconds: int) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_parse_jobs SET attempts=attempts+1, "
                "next_poll_at=now()+ %s * interval '1 second', updated_at=now() WHERE id=%s",
                (max(0, int(seconds)), job_id))

    def complete(self, job_id: str, result: dict[str, Any],
                 blocks: list[dict[str, Any]]) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_parse_jobs SET state='succeeded', result_json=%s, "
                "blocks_json=%s, completed_at=now(), updated_at=now(), error_code=NULL "
                "WHERE id=%s",
                (json.dumps(result, ensure_ascii=False, default=str),
                 json.dumps(blocks, ensure_ascii=False, default=str), job_id))

    def fail(self, job_id: str, code: str) -> None:
        with self.store.transaction() as cursor:
            cursor.execute(
                "UPDATE knowledge_parse_jobs SET state='failed', error_code=%s, "
                "completed_at=now(), updated_at=now() WHERE id=%s", (code, job_id))

    def retry_or_fail(self, job_id: str, code: str, *, max_attempts: int,
                      seconds: int) -> str:
        with self.store.transaction() as cursor:
            row = cursor.execute(
                "SELECT attempts FROM knowledge_parse_jobs WHERE id=%s FOR UPDATE",
                (job_id,)).fetchone()
            attempts = int(row["attempts"]) + 1 if row else max_attempts
            if attempts >= max_attempts:
                cursor.execute(
                    "UPDATE knowledge_parse_jobs SET state='failed', error_code=%s, "
                    "attempts=%s, completed_at=now(), updated_at=now() WHERE id=%s",
                    (code, attempts, job_id))
                return "failed"
            cursor.execute(
                "UPDATE knowledge_parse_jobs SET attempts=%s, error_code=%s, "
                "next_poll_at=now()+ %s * interval '1 second', updated_at=now() WHERE id=%s",
                (attempts, code, max(5, int(seconds)), job_id))
            return "retrying"


class ParseWorker:
    """One tick submits new jobs and advances submitted ones."""

    def __init__(self, jobs: ParseJobStore, client: ParseClient, *,
                 poll_seconds: int = 10, backoff_seconds: int = 30,
                 max_attempts: int = 5, timeout_seconds: int = 1800,
                 on_complete=None, usage: DocMindUsageStore | None = None,
                 images: DocumentImageStore | None = None) -> None:
        self.jobs = jobs
        self.client = client
        # Usage accounting must never fail a parse; it is best effort.
        self.usage = usage if usage is not None else (
            DocMindUsageStore(jobs.store) if getattr(jobs, "store", None) else None)
        # Image persistence is opt-in by deployment configuration; without a
        # tenant storage root the signed links are only stripped.
        self.images = images if images is not None else (
            DocumentImageStore.from_env(jobs.store)
            if getattr(jobs, "store", None) else None)
        self.poll_seconds = poll_seconds
        self.backoff_seconds = backoff_seconds
        self.max_attempts = max_attempts
        self.timeout_seconds = timeout_seconds
        self.on_complete = on_complete

    def tick(self, *, limit: int = 5,
             tenant_id: str | None = None) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for job in self.jobs.claim(limit=limit, tenant_id=tenant_id):
            try:
                results.append(self._advance(job))
            except DocMindError as error:
                state = self.jobs.retry_or_fail(
                    job["id"], error.code, max_attempts=self.max_attempts,
                    seconds=self.backoff_seconds)
                results.append({"id": job["id"], "state": state, "error": error.code})
        return {"processed": len(results), "results": results}

    def _record(self, job: dict[str, Any], **values: Any) -> dict[str, Any] | None:
        if self.usage is None:
            return None
        try:
            return self.usage.record(str(job["tenant_id"]), **values)
        except Exception:  # noqa: BLE001 - accounting must not fail the parse
            return None

    def _advance(self, job: dict[str, Any]) -> dict[str, Any]:
        if _timed_out(job, self.timeout_seconds):
            self.jobs.fail(job["id"], "docmind_timeout")
            return {"id": job["id"], "state": "failed", "error": "docmind_timeout"}
        if not job.get("vendor_job_id"):
            conversion = _json(job.get("conversion_json"))
            upload_path = str(conversion.get("upload_path")
                              or conversion.get("original_path")
                              or job["source_path"])
            filename = str(conversion.get("upload_filename")
                           or conversion.get("filename") or job["source_path"])
            extension = str(conversion.get("extension")
                            or Path(upload_path).suffix.lstrip(".")).lower()
            try:
                vendor_id = self.client.submit(upload_path, filename, extension)
            finally:
                # Every submission attempt is a vendor call even when it fails.
                self._record(job, calls=1)
            self.jobs.submitted(job["id"], vendor_id, seconds=self.poll_seconds)
            return {"id": job["id"], "state": "submitted", "vendor_job_id": vendor_id}
        status = self.client.status(str(job["vendor_job_id"]))
        kind = str(status.get("status") or "").lower()
        if kind in ("success", "succeeded", "done"):
            result = self.client.result(str(job["vendor_job_id"]))
            conversion = _json(job.get("conversion_json"))
            blocks = layouts_to_blocks(
                result, document_id=str(job["document_id"]),
                version=int(job["document_version"]),
                source_sha256=str(job["source_sha256"]),
                image_store=self.images, image_tenant=str(job["tenant_id"]),
                acl_scope=str(conversion.get("acl_scope") or "project"))
            written = None
            if self.on_complete is not None:
                written = self.on_complete(job, result, blocks)
            pages = max([int(block.get("page") or 0) for block in blocks] or [0])
            usage = self._record(job, pages=pages, blocks=len(blocks),
                                 tokens=_tokens(result))
            self.jobs.complete(job["id"], result, blocks)
            return {"id": job["id"], "state": "succeeded", "pages": pages,
                    "blocks": len(blocks), "written": written, "usage": usage}
        if kind in ("fail", "failed", "error"):
            self.jobs.fail(job["id"], "docmind_parse_failed")
            return {"id": job["id"], "state": "failed", "error": "docmind_parse_failed"}
        self.jobs.poll_later(job["id"], seconds=self.poll_seconds)
        return {"id": job["id"], "state": "processing", "status": kind}


def _tokens(result: dict[str, Any]) -> int:
    for key in ("tokens", "total_tokens", "token_count"):
        value = result.get(key)
        if isinstance(value, (int, float)) and value >= 0:
            return int(value)
    usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
    value = usage.get("total_tokens") or usage.get("tokens")
    return int(value) if isinstance(value, (int, float)) and value >= 0 else 0


def _timed_out(job: dict[str, Any], seconds: int) -> bool:
    submitted = job.get("submitted_at") or job.get("created_at")
    if not submitted:
        return False
    if isinstance(submitted, str):
        try:
            submitted = datetime.fromisoformat(submitted.replace("Z", "+00:00"))
        except ValueError:
            return False
    if submitted.tzinfo is None:
        submitted = submitted.replace(tzinfo=UTC)
    return (_now() - submitted) > timedelta(seconds=seconds)


def _json(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}

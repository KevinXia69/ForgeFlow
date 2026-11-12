"""Persist Document Mind images inside tenant storage with an image-level ACL.

The vendor returns short-lived signed URLs.  Storing the URL would leak an STS
token into blocks, logs and the vector store, so the bytes are downloaded while
the link is valid and addressed by their content hash afterwards.

Safety boundaries:

* only ``https`` URLs on the vendor's Aliyun hosts are fetched; a foreign host,
  a non-443 port, userinfo or a redirect is refused;
* at most ``max_bytes`` per image and ``max_images`` per document;
* writes are atomic, private (0600 file, 0700 directory) and content-addressed,
  so repeated parses of the same document reuse one file;
* the stored row carries the document's ACL scope; reading requires the caller's
  role to include it.  A missing row or another tenant's hash reads as absent.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import httpx

from ..superagent.store import ControlStore

MICROS = 1_000_000
ALLOWED_SUFFIXES = (".aliyuncs.com", ".aliyuncs.com.cn")
IMAGE_TYPES = {
    "image/png": ".png", "image/jpeg": ".jpg", "image/jpg": ".jpg",
    "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
    "image/tiff": ".tiff",
}
_SHA = re.compile(r"^[0-9a-f]{64}$")


class ImageStoreError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class StoredImage:
    sha256: str
    content_type: str
    byte_size: int
    storage_path: str


@dataclass(frozen=True, slots=True)
class ImageRecord:
    sha256: str
    document_id: str
    document_version: int
    source_sha256: str
    acl_scope: str
    content_type: str
    byte_size: int
    storage_path: str


def _content_type(value: str) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _validate_url(url: str, allowed_suffixes: tuple[str, ...]) -> None:
    parsed = urlsplit(str(url or ""))
    if parsed.scheme != "https":
        raise ImageStoreError("image_url_scheme_rejected")
    host = (parsed.hostname or "").lower()
    if not host or "@" in parsed.netloc:
        raise ImageStoreError("image_url_host_rejected")
    if parsed.port not in (None, 443):
        raise ImageStoreError("image_url_port_rejected")
    if not any(host == suffix.lstrip(".") or host.endswith(suffix)
               for suffix in allowed_suffixes):
        raise ImageStoreError("image_url_host_rejected")


def _default_fetch(url: str, *, max_bytes: int) -> tuple[str, bytes]:
    try:
        with httpx.Client(timeout=15, follow_redirects=False, trust_env=False) as client:
            with client.stream("GET", url) as response:
                if response.status_code != 200:
                    raise ImageStoreError("image_download_failed")
                content_type = _content_type(response.headers.get("content-type", ""))
                if content_type not in IMAGE_TYPES:
                    raise ImageStoreError("image_content_type_rejected")
                chunks: list[bytes] = []
                total = 0
                for chunk in response.iter_bytes():
                    total += len(chunk)
                    if total > max_bytes:
                        raise ImageStoreError("image_too_large")
                    chunks.append(chunk)
                return content_type, b"".join(chunks)
    except httpx.HTTPError:
        raise ImageStoreError("image_download_failed") from None


class DocumentImageStore:
    """Content-addressed image storage plus its tenant/document metadata."""

    def __init__(self, store: ControlStore, root: str | Path, *,
                 max_bytes: int = 10 * 1024 * 1024, max_images: int = 50,
                 allowed_suffixes: tuple[str, ...] = ALLOWED_SUFFIXES,
                 fetch: Callable[..., tuple[str, bytes]] | None = None) -> None:
        self.store = store
        self.root = Path(root)
        self.max_bytes = int(max_bytes)
        self.max_images = int(max_images)
        self.allowed_suffixes = tuple(allowed_suffixes)
        self._fetch = fetch or _default_fetch

    @classmethod
    def from_env(cls, store: ControlStore) -> "DocumentImageStore | None":
        root = os.environ.get("FORGEFLOW_DOCUMENT_IMAGE_ROOT", "").strip()
        if not root:
            return None
        return cls(store, root)

    def persist(self, url: str, *, tenant_id: str, document_id: str,
                version: int = 1, source_sha256: str = "",
                acl_scope: str = "project") -> StoredImage:
        if not tenant_id or not document_id:
            raise ImageStoreError("image_owner_required")
        _validate_url(url, self.allowed_suffixes)
        content_type, payload = self._fetch(url, max_bytes=self.max_bytes)
        content_type = _content_type(content_type)
        if content_type not in IMAGE_TYPES:
            raise ImageStoreError("image_content_type_rejected")
        if not payload:
            raise ImageStoreError("image_empty")
        if len(payload) > self.max_bytes:
            raise ImageStoreError("image_too_large")
        digest = hashlib.sha256(payload).hexdigest()
        existing = self._links(tenant_id, digest)
        relative = (existing[0].storage_path if existing else
                    str(Path(tenant_id) / digest[:2]
                        / f"{digest}{IMAGE_TYPES[content_type]}"))
        target = self.root / relative
        if not target.is_file():
            self._write(target, payload)
        try:
            with self.store.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO knowledge_document_images(tenant_id, sha256, "
                    "document_id, document_version, source_sha256, acl_scope, "
                    "content_type, byte_size, storage_path) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (tenant_id, sha256, document_id, document_version) "
                    "DO UPDATE SET acl_scope=EXCLUDED.acl_scope, "
                    "content_type=EXCLUDED.content_type, byte_size=EXCLUDED.byte_size",
                    (tenant_id, digest, document_id, int(version), source_sha256,
                     str(acl_scope), content_type, len(payload), relative))
        except Exception:
            if not existing:
                target.unlink(missing_ok=True)
            raise
        return StoredImage(digest, content_type, len(payload), relative)

    def read(self, *, tenant_id: str, sha256: str,
             acl_scopes: frozenset[str] | set[str]) -> tuple[ImageRecord, bytes] | None:
        if not _SHA.match(str(sha256 or "")):
            return None
        allowed = set(acl_scopes)
        for record in self._links(tenant_id, str(sha256)):
            if record.acl_scope not in allowed:
                continue
            path = self.root / record.storage_path
            try:
                return record, path.read_bytes()
            except OSError:
                return None
        return None

    def delete_for_document(self, *, tenant_id: str, document_id: str,
                            version: int | None = None) -> int:
        domain = "tenant_id=%s AND document_id=%s"
        params: list[Any] = [tenant_id, document_id]
        if version is not None:
            domain += " AND document_version=%s"
            params.append(int(version))
        rows = self.store.connection.execute(
            f"SELECT sha256, storage_path FROM knowledge_document_images WHERE {domain}",
            params).fetchall()
        self.store.connection.commit()
        with self.store.transaction() as cursor:
            cursor.execute(
                f"DELETE FROM knowledge_document_images WHERE {domain}", params)
        removed = 0
        for row in rows:
            remaining = self.store.connection.execute(
                "SELECT count(*) AS count FROM knowledge_document_images "
                "WHERE tenant_id=%s AND sha256=%s",
                (tenant_id, str(row["sha256"]))).fetchone()
            self.store.connection.commit()
            if int(remaining["count"]) == 0:
                (self.root / str(row["storage_path"])).unlink(missing_ok=True)
            removed += 1
        return removed

    def delete_for_source(self, *, tenant_id: str,
                          source_sha256: str) -> int:
        """Remove every image link belonging to a source document hash."""
        if not source_sha256:
            return 0
        rows = self.store.connection.execute(
            "SELECT sha256, storage_path FROM knowledge_document_images "
            "WHERE tenant_id=%s AND source_sha256=%s", (tenant_id, source_sha256)).fetchall()
        self.store.connection.commit()
        with self.store.transaction() as cursor:
            cursor.execute(
                "DELETE FROM knowledge_document_images WHERE tenant_id=%s "
                "AND source_sha256=%s", (tenant_id, source_sha256))
        removed = 0
        for row in rows:
            remaining = self.store.connection.execute(
                "SELECT count(*) AS count FROM knowledge_document_images "
                "WHERE tenant_id=%s AND sha256=%s",
                (tenant_id, str(row["sha256"]))).fetchone()
            self.store.connection.commit()
            if int(remaining["count"]) == 0:
                (self.root / str(row["storage_path"])).unlink(missing_ok=True)
            removed += 1
        return removed

    # -- helpers -------------------------------------------------------------
    def _links(self, tenant_id: str, sha256: str) -> list[ImageRecord]:
        rows = self.store.connection.execute(
            "SELECT sha256, document_id, document_version, source_sha256, acl_scope, "
            "content_type, byte_size, storage_path FROM knowledge_document_images "
            "WHERE tenant_id=%s AND sha256=%s ORDER BY created_at",
            (tenant_id, sha256)).fetchall()
        self.store.connection.commit()
        return [ImageRecord(str(row["sha256"]), str(row["document_id"]),
                            int(row["document_version"]),
                            str(row["source_sha256"] or ""), str(row["acl_scope"]),
                            str(row["content_type"]), int(row["byte_size"]),
                            str(row["storage_path"])) for row in rows]

    def _write(self, target: Path, payload: bytes) -> None:
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_suffix(target.suffix + ".tmp")
        temporary.write_bytes(payload)
        temporary.chmod(0o600)
        temporary.replace(target)

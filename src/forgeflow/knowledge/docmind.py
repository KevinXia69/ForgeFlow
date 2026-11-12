"""Alibaba Cloud Document Mind (大模型版) client and block conversion.

All enterprise knowledge documents are parsed by Document Mind.  This module is
the only place that calls the vendor: it uses the official Python SDK, loads
credentials through the standard environment chain, never logs them, and maps
the returned layouts to ForgeFlow document blocks with their original page and
layout position.  No local PDF/DOCX/XLSX parser is used as a substitute.
"""
from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Extensions Document Mind accepts directly.  Everything else is converted by
#: ``docmind_convert`` before it reaches this client.
DIRECT_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "png", "jpg", "jpeg",
    "bmp", "gif", "webp", "md", "markdown", "txt", "html", "htm",
})

ENV_KEYS = ("ALIBABA_CLOUD_ACCESS_KEY_ID", "ALIBABA_CLOUD_ACCESS_KEY_SECRET")
BAILIAN_BASE = "https://dashscope.aliyuncs.com"


class DocMindError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DocMindConfig:
    endpoint: str
    region: str
    access_key_id: str = ""
    access_key_secret: str = ""
    security_token: str = ""
    api_key: str = ""
    workspace_id: str = ""
    mode: str = "acs"

    @classmethod
    def from_env(cls, environ=None) -> "DocMindConfig | None":
        env = os.environ if environ is None else environ
        # Bailian (Model Studio) is the deployment's Alibaba credential: its
        # sk- key calls the same Document Mind parser through DashScope.
        api_key = env.get("DASHSCOPE_API_KEY", "")
        if api_key.startswith("sk-"):
            return cls(
                endpoint=(env.get("FORGEFLOW_DOCMIND_ENDPOINT")
                          or env.get("DASHSCOPE_BASE_URL") or BAILIAN_BASE).strip(),
                region=(env.get("FORGEFLOW_DOCMIND_REGION") or "cn-beijing").strip(),
                api_key=api_key,
                workspace_id=env.get("DASHSCOPE_WORKSPACE_ID", ""),
                mode="bailian")
        key_id = (env.get("ALIBABA_CLOUD_ACCESS_KEY_ID")
                  or env.get("ALIDOCMIND_ACCESS_KEY_ID") or "")
        key_secret = (env.get("ALIBABA_CLOUD_ACCESS_KEY_SECRET")
                      or env.get("ALIDOCMIND_ACCESS_KEY_SECRET") or "")
        token = (env.get("ALIBABA_CLOUD_SECURITY_TOKEN")
                 or env.get("ALIDOCMIND_SECURITY_TOKEN") or "")
        if not key_id or not key_secret:
            return None
        return cls(
            endpoint=(env.get("FORGEFLOW_DOCMIND_ENDPOINT")
                      or "docmind-api.cn-hangzhou.aliyuncs.com").strip(),
            region=(env.get("FORGEFLOW_DOCMIND_REGION") or "cn-hangzhou").strip(),
            access_key_id=key_id, access_key_secret=key_secret,
            security_token=token, mode="acs")

    @classmethod
    def missing(cls, environ=None) -> list[str]:
        env = os.environ if environ is None else environ
        if env.get("DASHSCOPE_API_KEY"):
            return []
        return [key for key in ENV_KEYS if not env.get(key)]

    def as_public(self) -> dict[str, str]:
        """Safe for logs and diagnostics: no credential material."""
        return {"endpoint": self.endpoint, "region": self.region,
                "mode": self.mode,
                "credential": ("bailian_api_key" if self.mode == "bailian"
                               else "access_key" if self.access_key_id else "missing"),
                "workspace": "present" if self.workspace_id else ""}


class DocumentMindClient:
    """Thin SDK wrapper; the SDK is imported only when a call is made."""

    def __init__(self, config: DocMindConfig, *, client=None) -> None:
        self.config = config
        self._client = client

    def _sdk(self):
        if self._client is not None:
            return self._client
        try:
            from alibabacloud_docmind_api20220711.client import Client
            from alibabacloud_tea_openapi import models as open_api_models
        except ImportError:
            raise DocMindError("docmind_sdk_missing") from None
        config = open_api_models.Config(
            access_key_id=self.config.access_key_id,
            access_key_secret=self.config.access_key_secret,
            security_token=self.config.security_token or None,
            endpoint=self.config.endpoint,
            region_id=self.config.region,
        )
        self._client = Client(config)
        return self._client

    @staticmethod
    def _runtime():
        from alibabacloud_tea_util import models as util_models
        return util_models.RuntimeOptions(connect_timeout=30_000,
                                          read_timeout=300_000)

    def submit(self, path: str | Path, filename: str, extension: str, *,
               llm_enhancement: bool = True,
               output_html_table: bool = True) -> str:
        if self.config.mode == "bailian":
            return self._bailian_submit(Path(path), filename)
        try:
            from alibabacloud_docmind_api20220711 import models as dm
        except ImportError:
            raise DocMindError("docmind_sdk_missing") from None
        runtime = self._runtime()
        try:
            with Path(path).open("rb") as handle:
                request = dm.SubmitDocParserJobAdvanceRequest(
                    file_name=filename, file_name_extension=extension,
                    file_url_object=handle,
                    llm_enhancement=bool(llm_enhancement),
                    output_html_table=bool(output_html_table))
                response = self._sdk().submit_doc_parser_job_advance(request, runtime)
        except DocMindError:
            raise
        except Exception as error:  # noqa: BLE001 - vendor exceptions are opaque
            raise DocMindError(_vendor_code(error, "docmind_submit_failed")) from None
        job_id = getattr(getattr(response, "body", None), "data", None)
        job_id = getattr(job_id, "id", None)
        if not job_id:
            raise DocMindError("docmind_job_id_missing")
        return str(job_id)

    def status(self, job_id: str) -> dict[str, Any]:
        if self.config.mode == "bailian":
            return self._bailian_status(job_id)
        try:
            from alibabacloud_docmind_api20220711 import models as dm
        except ImportError:
            raise DocMindError("docmind_sdk_missing") from None
        try:
            response = self._sdk().query_doc_parser_status(
                dm.QueryDocParserStatusRequest(id=job_id), self._runtime())
        except Exception as error:  # noqa: BLE001
            raise DocMindError(_vendor_code(error, "docmind_query_failed")) from None
        data = getattr(getattr(response, "body", None), "data", None)
        return {
            "status": str(getattr(data, "status", "") or "").lower(),
            "page_count": getattr(data, "page_count_estimate", None),
            "paragraph_count": getattr(data, "paragraph_count", None),
            "table_count": getattr(data, "table_count", None),
            "image_count": getattr(data, "image_count", None),
            "tokens": getattr(data, "tokens", None),
            "processing": getattr(data, "processing", None),
            "message": str(getattr(getattr(response, "body", None), "message", "") or ""),
        }

    def result(self, job_id: str, *, page_size: int = 100,
               max_layouts: int = 100_000) -> dict[str, Any]:
        if self.config.mode == "bailian":
            return self._bailian_result(job_id)
        try:
            from alibabacloud_docmind_api20220711 import models as dm
        except ImportError:
            raise DocMindError("docmind_sdk_missing") from None
        client = self._sdk()
        merged: dict[str, Any] = {}
        layouts: list[Any] = []
        offset = 0
        while True:
            try:
                response = client.get_doc_parser_result(
                    dm.GetDocParserResultRequest(
                        id=job_id, layout_num=offset, layout_step_size=page_size),
                    self._runtime())
            except Exception as error:  # noqa: BLE001
                raise DocMindError(_vendor_code(error, "docmind_result_failed")) from None
            data = getattr(getattr(response, "body", None), "data", None) or {}
            page = data.get("layouts") if isinstance(data, dict) else None
            page = page if isinstance(page, list) else []
            if not page and offset > 0:
                break
            layouts.extend(page)
            for key, value in (data.items() if isinstance(data, dict) else []):
                if key not in ("layouts", "segments") and key not in merged:
                    merged[key] = value
            if len(page) < page_size:
                break
            offset += len(page)
            if len(layouts) >= max_layouts:
                merged["truncated"] = True
                break
        merged["layouts"] = layouts
        return merged


    # -- Bailian / Model Studio (DashScope API key) ----------------------
    def _bailian_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.config.api_key}",
            "Content-Type": "application/json",
            "X-DashScope-WorkSpace": self.config.workspace_id,
            "X-DashScope-OpenAPISource": "CloudSDK",
        }

    def _bailian_url(self, suffix: str) -> str:
        return (f"{self.config.endpoint.rstrip('/')}/api/v1/datacenter/"
                f"category/default/{suffix}")

    def _bailian_post(self, url: str, payload: dict) -> dict:
        import httpx
        try:
            response = httpx.post(url, headers=self._bailian_headers(),
                                  json=payload, timeout=120)
        except httpx.HTTPError:
            raise DocMindError("docmind_unavailable") from None
        if response.status_code in (401, 403):
            raise DocMindError("docmind_auth_failed")
        if response.status_code == 429:
            raise DocMindError("docmind_rate_limited")
        if response.status_code != 200:
            raise DocMindError("docmind_query_failed")
        try:
            body = response.json()
        except ValueError:
            raise DocMindError("docmind_invalid_response") from None
        if str(body.get("code", "")).lower() != "success":
            raise DocMindError(_vendor_code(
                RuntimeError(str(body.get("message") or "failed")),
                "docmind_query_failed"))
        return body.get("data") or {}

    def _bailian_submit(self, path: Path, filename: str) -> str:
        import hashlib

        import httpx
        raw = path.read_bytes()
        lease = self._bailian_post(self._bailian_url("upload_lease"), {
            "file_name": filename, "size_bytes": len(raw),
            "content_md5": hashlib.md5(raw).hexdigest()})
        param = lease.get("param") or {}
        try:
            upload = httpx.request(str(param.get("method") or "PUT"),
                                   str(param.get("url") or ""),
                                   content=raw,
                                   headers=param.get("headers") or {},
                                   timeout=300)
        except httpx.HTTPError:
            raise DocMindError("docmind_unavailable") from None
        if upload.status_code != 200:
            raise DocMindError("docmind_upload_failed")
        added = self._bailian_post(self._bailian_url("add_file"), {
            "lease_id": lease.get("lease_id"), "parser": "DASHSCOPE_DOCMIND"})
        if not added.get("file_id"):
            raise DocMindError("docmind_job_id_missing")
        return str(added["file_id"])

    def _bailian_status(self, job_id: str) -> dict[str, Any]:
        data = self._bailian_post(self._bailian_url(f"file/{job_id}/query"),
                                  {"file_id": job_id})
        raw = str(data.get("status") or "").upper()
        mapped = {"PARSE_SUCCESS": "success", "PARSING": "processing",
                  "INIT": "processing"}.get(raw, "failed")
        return {"status": mapped, "raw_status": raw,
                "page_count": None, "paragraph_count": None,
                "table_count": None, "image_count": None, "tokens": None,
                "processing": None, "message": ""}

    def _bailian_result(self, job_id: str) -> dict[str, Any]:
        import httpx
        lease = self._bailian_post(self._bailian_url(f"file/{job_id}/download_lease"),
                                   {"file_id": job_id})
        param = lease.get("param") or {}
        try:
            response = httpx.request(str(param.get("method") or "GET"),
                                     str(param.get("url") or ""),
                                     headers=param.get("headers") or {},
                                     timeout=300)
        except httpx.HTTPError:
            raise DocMindError("docmind_unavailable") from None
        if response.status_code != 200:
            raise DocMindError("docmind_result_failed")
        try:
            value = response.json()
        except ValueError:
            raise DocMindError("docmind_result_invalid") from None
        return value if isinstance(value, dict) else {"layouts": []}

def _vendor_code(error: Exception, fallback: str) -> str:
    text = f"{type(error).__name__}: {error}"
    lowered = text.lower()
    if "accesskey" in lowered or "signature" in lowered or "forbidden" in lowered:
        return "docmind_auth_failed"
    if "throttl" in lowered or "rate" in lowered:
        return "docmind_rate_limited"
    if "timeout" in lowered or "unavailable" in lowered or "connect" in lowered:
        return "docmind_unavailable"
    return fallback


def layouts_to_blocks(result: dict[str, Any], *, document_id: str, version: int,
                      source_sha256: str, image_store=None, image_tenant: str = "",
                      acl_scope: str = "project") -> list[dict[str, Any]]:
    """Convert Document Mind layouts to ForgeFlow blocks.

    Only fields the vendor actually returned are kept.  A layout without a page
    number gets a document-level locator instead of an invented page, and a
    layout without ``pos`` gets no coordinates.
    """
    blocks: list[dict[str, Any]] = []
    persisted: dict[str, Any] = {"images": [], "errors": []}
    layouts = result.get("layouts") if isinstance(result.get("layouts"), list) else []
    for index, layout in enumerate(layouts):
        if not isinstance(layout, dict):
            continue
        text = (layout.get("markdownContent") or layout.get("text")
                or layout.get("llmResult") or "")
        if not isinstance(text, str) or not text.strip():
            continue
        if image_store is not None and image_tenant:
            text = _persist_signed_images(
                text, store=image_store, tenant_id=image_tenant,
                document_id=document_id, version=version,
                source_sha256=source_sha256, acl_scope=acl_scope,
                state=persisted)
        else:
            text = _without_signed_links(text)
        page = layout.get("pageNum")
        if isinstance(page, list) and page:
            page = page[0]
        page_number = int(page) + 1 if isinstance(page, int) and page >= 0 else None
        kind = _block_type(layout.get("type"), layout.get("level"))
        locator = (f"page:{page_number}#{index}" if page_number is not None
                   else f"doc:{document_id}@v{version}#{index}")
        position = layout.get("pos") if isinstance(layout.get("pos"), dict) else None
        blocks.append({
            "ord": len(blocks),
            "block_type": kind,
            "text": text.strip(),
            "locator": locator,
            "page": page_number,
            "pos": position,
            "confidence": layout.get("layoutConf"),
            "layout_type": str(layout.get("type") or ""),
            "document_id": document_id,
            "version": int(version),
            "source_sha256": source_sha256,
            "source": "document_mind",
        })
    # One document-level projection: a block only carries the images it names,
    # while the caller can also see every persisted image and failure.
    for block in blocks:
        refs = [item for item in persisted["images"]
                if item["sha256"] in block["text"]]
        if refs:
            block["images"] = refs
    if persisted["errors"] and blocks:
        # Failures stay visible without duplicating the same metadata in every
        # block (and therefore in the vector store).
        blocks[0]["image_errors"] = list(dict.fromkeys(persisted["errors"]))
    return blocks


#: Vendor-signed OSS links carry a temporary STS token in the query string.
#: Persisting them would leak credential material into blocks and the vector DB.
_SIGNED_IMAGE = re.compile(r"!\[[^\]]*\]\(([^)]*)\)")
_SIGNED_URL = re.compile(
    r"https?://[^\s)\]]*(?:security-token|OSSAccessKeyId|Signature|Expires)="
    r"[^\s)\]]*", re.IGNORECASE)
_SIGNED_MARKER = re.compile(
    r"(?:security-token|OSSAccessKeyId|Signature|Expires)=", re.IGNORECASE)


def _persist_signed_images(text: str, *, store, tenant_id: str, document_id: str,
                           version: int, source_sha256: str, acl_scope: str,
                           state: dict[str, Any]) -> str:
    """Download signed images while valid and replace them with their hash."""
    def replace(match: re.Match) -> str:
        url = match.group(1).strip()
        # CommonMark allows an image destination in angle brackets.  The vendor
        # uses that form; the brackets are not part of the URL.
        if url.startswith("<") and url.endswith(">"):
            url = url[1:-1].strip()
        if not _SIGNED_MARKER.search(url):
            return match.group(0)
        if len(state["images"]) >= int(getattr(store, "max_images", 50)):
            state["errors"].append("image_limit_reached")
            return "[image]"
        try:
            stored = store.persist(
                url, tenant_id=tenant_id, document_id=document_id, version=version,
                source_sha256=source_sha256, acl_scope=acl_scope)
        except Exception as error:  # noqa: BLE001 - download failure never fails parse
            state["errors"].append(str(getattr(error, "code", "image_persist_failed")))
            return "[image]"
        if not any(item["sha256"] == stored.sha256 for item in state["images"]):
            state["images"].append({"sha256": stored.sha256,
                                    "content_type": stored.content_type,
                                    "bytes": stored.byte_size})
        return f"[image:{stored.sha256}]"

    cleaned = _SIGNED_IMAGE.sub(replace, text)
    return _SIGNED_URL.sub("[provider file]", cleaned)


def _without_signed_links(text: str) -> str:
    def replace_image(match: re.Match) -> str:
        url = match.group(1)
        return "[image]" if _SIGNED_MARKER.search(url) else match.group(0)

    cleaned = _SIGNED_IMAGE.sub(replace_image, text)
    return _SIGNED_URL.sub("[provider file]", cleaned)


def _block_type(kind: Any, level: Any) -> str:
    value = str(kind or "").lower()
    if value == "title":
        return "section" if isinstance(level, int) and level <= 1 else "heading"
    if value in ("table", "cell", "row"):
        return "table" if value == "table" else value
    if value in ("head", "foot", "figcaption"):
        return "line"
    return "paragraph"


def source_sha256(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()

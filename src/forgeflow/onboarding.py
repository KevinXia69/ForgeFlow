"""Tenant-context onboarding for a general Odoo workbench.

The workspace stores sources and a reviewed field mapping.  Confirming the
mapping is **not** an ERP import: it records ``mapping_confirmed`` and a
``mapping_adoption`` summary.  Actual writes only happen through
``preview_import`` -> ``authorize_import`` -> ``apply_import`` -> ``readback_import``
in :mod:`forgeflow.onboarding_import`, and only after the operator authorizes the
exact preview digest.
"""
from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import io
import json
import re
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import onboarding_import

MAX_SOURCE_BYTES = 5 * 1024 * 1024
MAX_SOURCES = 12
MAX_ROWS = 5000
MAX_SAMPLE_ROWS = 3
MAX_CELL = 240
MAX_FIELDS = 500
WORKSPACE_ID = re.compile(r"^[a-f0-9]{32}$")
SAFE_EXTENSIONS = {".csv", ".tsv", ".json", ".txt", ".md", ".xlsx", ".pdf"}
TARGET_MODELS: dict[str, tuple[str, ...]] = {
    "res.partner": (
        "name", "ref", "email", "phone", "country_id", "supplier_rank",
        "customer_rank", "comment",
    ),
    "product.product": (
        "name", "default_code", "barcode", "list_price", "standard_price",
        "uom_id", "categ_id", "type",
    ),
    "sale.order": (
        "name", "partner_id", "date_order", "commitment_date", "client_order_ref",
        "amount_total", "state",
    ),
    "sale.order.line": (
        "order_id", "product_id", "product_uom_qty", "price_unit",
        "customer_lead",
    ),
    "purchase.order": (
        "name", "partner_id", "date_order", "date_planned", "amount_total",
        "state",
    ),
    "purchase.order.line": (
        "order_id", "product_id", "product_qty", "price_unit", "date_planned",
    ),
    "mrp.production": (
        "name", "product_id", "product_qty", "date_start", "date_finished",
        "state", "bom_id",
    ),
    "stock.warehouse": ("name", "code", "company_id"),
    "uom.uom": ("name", "factor"),
    "mrp.bom": ("code", "product_tmpl_id", "product_qty", "type", "company_id"),
    "mrp.bom.line": ("bom_id", "product_id", "product_qty"),
    "mrp.workcenter": ("name", "code", "capacity", "time_efficiency"),
    "forgeflow.exception_event": (
        "name", "event_type", "partner_id", "product_id", "occurred_at",
        "severity", "description",
    ),
}
_MODEL_LABELS = {
    "res.partner": "Partner",
    "product.product": "Product",
    "sale.order": "Sales order",
    "sale.order.line": "Sales order line",
    "purchase.order": "Purchase order",
    "purchase.order.line": "Purchase order line",
    "mrp.production": "Manufacturing order",
    "uom.uom": "Unit of measure",
    "mrp.bom": "Bill of materials",
    "mrp.bom.line": "BOM component",
    "mrp.workcenter": "Work center",
    "stock.warehouse": "Warehouse",
    "forgeflow.exception_event": "Exception event",
}
_ALIASES: dict[str, tuple[str, str]] = {
    "customer": ("res.partner", "name"),
    "customer_name": ("res.partner", "name"),
    "client": ("res.partner", "name"),
    "客户": ("res.partner", "name"),
    "客户名称": ("res.partner", "name"),
    "supplier": ("res.partner", "name"),
    "vendor": ("res.partner", "name"),
    "供应商": ("res.partner", "name"),
    "供应商名称": ("res.partner", "name"),
    "email": ("res.partner", "email"),
    "邮箱": ("res.partner", "email"),
    "phone": ("res.partner", "phone"),
    "电话": ("res.partner", "phone"),
    "product": ("product.product", "name"),
    "product_name": ("product.product", "name"),
    "item": ("product.product", "name"),
    "产品": ("product.product", "name"),
    "产品名称": ("product.product", "name"),
    "sku": ("product.product", "default_code"),
    "code": ("product.product", "default_code"),
    "物料编码": ("product.product", "default_code"),
    "barcode": ("product.product", "barcode"),
    "条码": ("product.product", "barcode"),
    "price": ("product.product", "list_price"),
    "list_price": ("product.product", "list_price"),
    "售价": ("product.product", "list_price"),
    "cost": ("product.product", "standard_price"),
    "standard_price": ("product.product", "standard_price"),
    "成本": ("product.product", "standard_price"),
    "qty": ("sale.order.line", "product_uom_qty"),
    "quantity": ("sale.order.line", "product_uom_qty"),
    "数量": ("sale.order.line", "product_uom_qty"),
    "order": ("sale.order", "name"),
    "order_id": ("sale.order", "name"),
    "so": ("sale.order", "name"),
    "销售订单": ("sale.order", "name"),
    "订单": ("sale.order", "name"),
    "date": ("sale.order", "date_order"),
    "order_date": ("sale.order", "date_order"),
    "日期": ("sale.order", "date_order"),
    "due": ("sale.order", "commitment_date"),
    "due_date": ("sale.order", "commitment_date"),
    "交期": ("sale.order", "commitment_date"),
    "po": ("purchase.order", "name"),
    "purchase_order": ("purchase.order", "name"),
    "采购订单": ("purchase.order", "name"),
    "mo": ("mrp.production", "name"),
    "manufacturing_order": ("mrp.production", "name"),
    "生产订单": ("mrp.production", "name"),
    "event": ("forgeflow.exception_event", "event_type"),
    "event_type": ("forgeflow.exception_event", "event_type"),
    "异常": ("forgeflow.exception_event", "event_type"),
    "异常类型": ("forgeflow.exception_event", "event_type"),
}


class OnboardingError(Exception):
    """A client-safe onboarding failure code."""

    def __init__(self, code: str, status: int = 400) -> None:
        super().__init__(code)
        self.code = code
        self.status = status


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _text(value: object, limit: int, field: str) -> str:
    if not isinstance(value, str):
        raise OnboardingError(f"invalid_{field}")
    text = " ".join(value.split()).strip()
    if not text:
        raise OnboardingError(f"invalid_{field}")
    if len(text) > limit:
        raise OnboardingError(f"invalid_{field}")
    return text


def _safe_name(value: object) -> str:
    if not isinstance(value, str):
        raise OnboardingError("invalid_filename")
    name = Path(value).name
    if not name or len(name) > 160 or name.startswith(".") or "/" in name or "\\" in name:
        raise OnboardingError("invalid_filename")
    if Path(name).suffix.lower() not in SAFE_EXTENSIONS:
        raise OnboardingError("unsupported_source")
    return name


def _extension(media_type: str, filename: str) -> str:
    suffix = Path(filename).suffix.lower()
    if suffix:
        return suffix
    return {
        "text/csv": ".csv",
        "text/tab-separated-values": ".tsv",
        "application/json": ".json",
        "text/plain": ".txt",
        "text/markdown": ".md",
        "application/pdf": ".pdf",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    }.get(media_type, "")


def _normalise_header(value: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "_", value.strip().casefold()).strip("_")


def _suggest(header: str) -> tuple[str, str, str] | None:
    key = _normalise_header(header)
    if key in _ALIASES:
        model, field = _ALIASES[key]
        return model, field, "header-match"
    for alias, target in _ALIASES.items():
        if alias and (key == alias or key.endswith("_" + alias) or key.startswith(alias + "_")):
            return target[0], target[1], "partial-match"
    return None


def _csv_preview(raw: bytes, suffix: str) -> tuple[list[str], int, list[list[str]]]:
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            text = raw.decode("gb18030")
        except UnicodeDecodeError:
            raise OnboardingError("source_not_utf8") from None
    delimiter = "\t" if suffix == ".tsv" else ","
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error:
        raise OnboardingError("source_unreadable") from None
    if not rows:
        raise OnboardingError("empty_source")
    headers = [cell[:120].strip() for cell in rows[0]]
    if not any(headers):
        raise OnboardingError("empty_source")
    headers = [header or f"column_{index + 1}" for index, header in enumerate(headers)]
    data = rows[1:]
    if len(data) > MAX_ROWS:
        raise OnboardingError("source_too_many_rows")
    samples = [
        [cell[:MAX_CELL] for cell in row[: len(headers)]]
        for row in data[:MAX_SAMPLE_ROWS]
    ]
    return headers, len(data), samples


def _json_rows(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict):
        rows = next((item for item in value.values() if isinstance(item, list)), [])
    elif isinstance(value, list):
        rows = value
    else:
        rows = []
    return [row for row in rows if isinstance(row, dict)]


def _json_preview(raw: bytes) -> tuple[list[str], int, list[list[str]]]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise OnboardingError("source_unreadable") from None
    rows = _json_rows(value)
    headers: list[str] = []
    for row in rows[:100]:
        for key in row:
            if key not in headers:
                headers.append(str(key)[:120])
    data = [[str(row.get(header, ""))[:MAX_CELL] for header in headers] for row in rows[:MAX_ROWS]]
    return headers, len(rows), data[:MAX_SAMPLE_ROWS]


def _decode_text(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            raise OnboardingError("source_not_utf8") from None


def _decode(content: object) -> bytes:
    if not isinstance(content, str) or not content:
        raise OnboardingError("invalid_source_content")
    payload = content.split(",", 1)[-1] if content.startswith("data:") else content
    try:
        raw = base64.b64decode(payload, validate=True)
    except (ValueError, binascii.Error):
        raise OnboardingError("invalid_source_content") from None
    if len(raw) > MAX_SOURCE_BYTES:
        raise OnboardingError("source_too_large", 413)
    if not raw:
        raise OnboardingError("empty_source")
    return raw


class OnboardingService:
    """Owner-scoped, filesystem-backed workspace context review."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if self.root.is_symlink():
            raise RuntimeError("onboarding root must not be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _dir(self, workspace_id: str) -> Path:
        if WORKSPACE_ID.fullmatch(workspace_id) is None:
            raise OnboardingError("workspace_not_found", 404)
        path = self.root / workspace_id
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            raise OnboardingError("workspace_not_found", 404)
        return path

    def _manifest(self, workspace_id: str) -> dict[str, Any]:
        path = self._dir(workspace_id) / "workspace.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise OnboardingError("workspace_not_found", 404) from None
        if not isinstance(value, dict) or value.get("id") != workspace_id:
            raise OnboardingError("workspace_not_found", 404)
        return value

    def _write(self, workspace_id: str, value: dict[str, Any]) -> None:
        folder = self._dir(workspace_id)
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = folder / "workspace.json"
        temporary = folder / ".workspace.tmp"
        temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(target)

    def _owned(self, workspace: dict[str, Any], uid: str) -> None:
        if workspace.get("owner") != uid:
            raise OnboardingError("workspace_not_found", 404)

    @staticmethod
    def _view(value: dict[str, Any]) -> dict[str, Any]:
        # Knowledge publication and rule activation have their own catalogues.
        # A workspace has no source/version bindings to those lifecycles; old
        # placeholder flags must not become claims about this import workflow.
        hidden = {"owner", "knowledge_published", "rules_effective"}
        view = {key: item for key, item in value.items() if key not in hidden}
        view["states"] = _derive_states(value)
        return view

    def list(self, uid: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path in self.root.iterdir():
            if not path.is_dir() or path.is_symlink() or WORKSPACE_ID.fullmatch(path.name) is None:
                continue
            try:
                value = json.loads((path / "workspace.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if value.get("owner") == uid:
                rows.append(self._view(value))
        rows.sort(key=lambda item: str(item.get("updated_at", "")), reverse=True)
        return rows

    def create(self, uid: str, name: str, industry: str, modules: list[str],
               description: str = "") -> dict[str, Any]:
        workspace_id = uuid.uuid4().hex
        now = _now()
        value = {
            "schema_version": 1,
            "id": workspace_id,
            "owner": uid,
            "name": _text(name, 120, "workspace_name"),
            "industry": _text(industry, 120, "industry"),
            "description": (description or "").strip()[:1000],
            "modules": sorted({str(item)[:80] for item in modules if str(item).strip()}),
            "status": "draft",
            "version": 0,
            "mapping_adoption": None,
            "import": None,
            "created_at": now,
            "updated_at": now,
            "sources": [],
            "mappings": [],
            "warnings": ["read_only_context"],
        }
        folder = self.root / workspace_id
        folder.mkdir(mode=0o700)
        self._write(workspace_id, value)
        return self._view(value)

    def get(self, uid: str, workspace_id: str) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        return self._view(value)

    def add_source(self, uid: str, workspace_id: str, filename: str,
                   media_type: str, content: str) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        if value.get("status") != "draft":
            raise OnboardingError("workspace_confirmed", 409)
        if len(value.get("sources", [])) >= MAX_SOURCES:
            raise OnboardingError("too_many_sources", 413)
        name = _safe_name(filename)
        suffix = _extension(media_type, name)
        if not suffix:
            raise OnboardingError("unsupported_source")
        raw = _decode(content)
        headers: list[str] = []
        rows = 0
        samples: list[list[str]] = []
        status = "review"
        if suffix in (".csv", ".tsv"):
            headers, rows, samples = _csv_preview(raw, suffix)
            status = "mapped" if headers else "review"
        elif suffix == ".json":
            headers, rows, samples = _json_preview(raw)
            status = "mapped" if headers else "review"
        else:
            samples = []
        if len(value.get("mappings", [])) + len(headers) > MAX_FIELDS:
            raise OnboardingError("too_many_fields", 413)
        source_id = uuid.uuid4().hex
        source_dir = self._dir(workspace_id) / "sources"
        source_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = source_dir / f"{source_id}{suffix}"
        path.write_bytes(raw)
        source = {
            "id": source_id,
            "filename": name,
            "media_type": media_type[:120] or "application/octet-stream",
            "size_bytes": len(raw),
            "kind": suffix.lstrip("."),
            "row_count": rows,
            "headers": headers,
            "samples": samples,
            "status": status,
        }
        value["sources"].append(source)
        mappings = []
        for index, header in enumerate(headers):
            suggestion = _suggest(header)
            mappings.append({
                "id": f"{source_id}:{index}",
                "source_id": source_id,
                "source_field": header,
                "sample": samples[0][index] if samples and index < len(samples[0]) else "",
                "target_model": suggestion[0] if suggestion else "",
                "target_field": suggestion[1] if suggestion else "",
                "confidence": suggestion[2] if suggestion else "needs-review",
                "status": "suggested" if suggestion else "unmapped",
            })
        value["mappings"].extend(mappings)
        value["status"] = "draft"
        value["version"] = 0
        value["updated_at"] = _now()
        self._write(workspace_id, value)
        return self._view(value)

    def intake_text(self, uid: str, workspace_id: str, title: str,
                    content: str) -> dict[str, Any]:
        """Record pasted material into the explicitly selected workspace.

        There is no "newest draft" fallback: implicit ownership is how a paste
        ends up attached to the wrong enterprise.
        """
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        if value.get("status") != "draft":
            raise OnboardingError("workspace_confirmed", 409)
        text = content if isinstance(content, str) else ""
        text = text.strip()
        if not text:
            raise OnboardingError("empty_source")
        if len(text.encode("utf-8")) > MAX_SOURCE_BYTES:
            raise OnboardingError("source_too_large", 413)
        heading = " ".join(str(title or "").split()).strip()[:120]
        filename = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff._-]+", "-", heading).strip("-._") or "material"
        filename = f"{filename[:100]}.md"
        payload = base64.b64encode(text.encode("utf-8")).decode()
        return self.add_source(uid, workspace_id, filename, "text/markdown", payload)

    def confirm(self, uid: str, workspace_id: str, mapping_updates: list[Any]) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        if value.get("status") != "draft":
            raise OnboardingError("workspace_confirmed", 409)
        existing = {row["id"]: row for row in value.get("mappings", [])}
        if not existing:
            raise OnboardingError("no_mappings")
        if not isinstance(mapping_updates, list) or not mapping_updates:
            raise OnboardingError("mapping_updates_required")
        seen: set[str] = set()
        for update in mapping_updates:
            if not isinstance(update, dict):
                raise OnboardingError("invalid_mapping")
            mapping_id = update.get("id")
            if mapping_id not in existing or mapping_id in seen:
                raise OnboardingError("invalid_mapping")
            seen.add(mapping_id)
            row = existing[mapping_id]
            ignored = update.get("ignored") is True
            if ignored:
                row["status"] = "ignored"
                row["target_model"] = ""
                row["target_field"] = ""
                continue
            model = update.get("target_model")
            field = update.get("target_field")
            if model not in TARGET_MODELS or field not in TARGET_MODELS[model]:
                raise OnboardingError("invalid_mapping_target")
            row["target_model"] = model
            row["target_field"] = field
            row["status"] = "confirmed"
        undecided = [row for row in value.get("mappings", []) if row.get("status") not in ("confirmed", "ignored")]
        if undecided:
            raise OnboardingError("mappings_incomplete", 409)
        # Confirming the mapping adopts it as context.  It does not write any
        # ERP record; that requires the explicit import preview and authorization
        # below.
        value["status"] = "mapping_confirmed"
        value["version"] = 1
        value["confirmed_at"] = _now()
        value["updated_at"] = _now()
        value["mapping_adoption"] = self._adopt_mappings(value)
        value["import"] = None
        warnings = ["mapping_confirmed_not_imported"]
        ignored = sum(1 for row in value["mappings"] if row.get("status") == "ignored")
        if ignored:
            warnings.append(f"mappings_ignored:{ignored}")
        value["warnings"] = warnings
        self._write(workspace_id, value)
        return self._view(value)

    def _adopt_mappings(self, value: dict[str, Any]) -> dict[str, Any]:
        """Materialise the confirmed mapping into the context downstream reads.

        Deterministic on purpose: the same confirmed mapping yields the same
        digest, so re-reading a workspace cannot silently drift from what was
        confirmed, and a later import can detect that it changed.
        """
        accepted = [row for row in value.get("mappings", [])
                    if row.get("status") == "confirmed"]
        by_model: dict[str, list[dict[str, str]]] = {}
        for row in accepted:
            by_model.setdefault(str(row["target_model"]), []).append({
                "field": str(row["target_field"]),
                "source": str(row.get("source_column") or ""),
                "source_id": str(row.get("source_id") or ""),
            })
        for fields in by_model.values():
            fields.sort(key=lambda item: (item["field"], item["source"], item["source_id"]))
        canonical = json.dumps(by_model, sort_keys=True, separators=(",", ":"))
        return {
            "status": "adopted",
            "adopted_at": _now(),
            "models": sorted(by_model),
            "counts": {model: len(fields) for model, fields in sorted(by_model.items())},
            "mapping_sha256": hashlib.sha256(canonical.encode()).hexdigest(),
        }


    # ------------------------------------------------------------- import
    def _source_records(self, value: dict[str, Any]) -> dict[str, dict[str, Any]]:
        """The full stored rows for each tabular source, not the preview samples."""
        records: dict[str, dict[str, Any]] = {}
        folder = self._dir(value["id"]) / "sources"
        for source in value.get("sources", []):
            kind = str(source.get("kind") or "")
            path = folder / f"{source['id']}.{kind}"
            if not path.is_file():
                continue
            headers = list(source.get("headers") or [])
            if kind in ("csv", "tsv"):
                reader = csv.reader(io.StringIO(_decode_text(path.read_bytes())),
                                    delimiter="\t" if kind == "tsv" else ",")
                try:
                    rows = list(reader)[1:]
                except csv.Error:
                    raise OnboardingError("source_unreadable") from None
            elif kind == "json":
                try:
                    rows = _json_rows(json.loads(path.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    raise OnboardingError("source_unreadable") from None
                rows = [[str(row.get(header, ""))[:MAX_CELL] for header in headers]
                        for row in rows]
            else:
                continue
            rows = [row for row in rows
                    if any(str(cell).strip() for cell in row)][:MAX_ROWS]
            records[str(source["id"])] = {"headers": headers, "rows": rows}
        return records

    def preview_import(self, uid: str, workspace_id: str, adapter) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        if value.get("status") != "mapping_confirmed":
            raise OnboardingError("mapping_not_confirmed", 409)
        try:
            preview = onboarding_import.build_preview(
                value, self._source_records(value), adapter)
        except onboarding_import.ImportError as error:
            raise OnboardingError(error.code, 409) from None
        value["import"] = {"preview": preview,
                           "preview_sha256": preview["preview_sha256"],
                           "authorized": None, "execution": None,
                           "readback": None}
        value["updated_at"] = _now()
        self._write(workspace_id, value)
        return self._view(value)

    def authorize_import(self, uid: str, workspace_id: str,
                         preview_sha256: str) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        block = value.get("import") or {}
        preview = block.get("preview")
        if value.get("status") != "mapping_confirmed" or not preview:
            raise OnboardingError("import_preview_required", 409)
        try:
            authorization = onboarding_import.authorize(
                preview, uid=uid, preview_sha256=str(preview_sha256), now=_now())
        except onboarding_import.ImportError as error:
            raise OnboardingError(error.code, 409) from None
        block["authorized"] = authorization
        value["import"] = block
        value["status"] = "import_authorized"
        value["updated_at"] = _now()
        self._write(workspace_id, value)
        return self._view(value)

    def apply_import(self, uid: str, workspace_id: str, preview_sha256: str,
                     adapter) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        block = value.get("import") or {}
        preview = block.get("preview")
        authorized = block.get("authorized")
        retry_partial = (value.get("status") == "readback_failed"
                         and int(((block.get("execution") or {}).get("counts") or {}).get(
                             "failed", 0)) > 0)
        if value.get("status") in ("imported", "readback_passed", "readback_failed") \
                and not retry_partial:
            # A repeated submission of an already-applied preview reads the
            # original execution; it never writes a second document.
            if str(preview_sha256) == str(block.get("preview_sha256")):
                return self._view(value)
            raise OnboardingError("import_preview_changed", 409)
        if (value.get("status") not in ("import_authorized", "import_partial")
                and not retry_partial) or not preview:
            raise OnboardingError("import_not_authorized", 409)
        if str(preview_sha256) != str(block.get("preview_sha256")):
            raise OnboardingError("import_preview_changed", 409)
        previous = (block.get("execution") or {}).get("rows") or []
        try:
            execution = onboarding_import.execute(
                preview, authorized=authorized or {}, adapter=adapter,
                previous=previous, now=_now())
        except onboarding_import.ImportError as error:
            raise OnboardingError(error.code, 409) from None
        block["execution"] = execution
        value["import"] = block
        value["status"] = execution["status"]
        value["updated_at"] = _now()
        failed = int((execution.get("counts") or {}).get("failed", 0))
        warnings = ["mapping_confirmed_not_imported"]
        if failed:
            warnings.append(f"import_failed:{failed}")
        value["warnings"] = warnings
        self._write(workspace_id, value)
        return self._view(value)

    def readback_import(self, uid: str, workspace_id: str, adapter) -> dict[str, Any]:
        value = self._manifest(workspace_id)
        self._owned(value, uid)
        block = value.get("import") or {}
        preview = block.get("preview")
        execution = block.get("execution")
        if value.get("status") not in ("imported", "import_partial", "readback_failed") \
                or not preview or not execution:
            raise OnboardingError("import_required", 409)
        result = onboarding_import.readback(preview, execution, adapter)
        block["readback"] = result
        value["import"] = block
        value["status"] = result["status"]
        value["updated_at"] = _now()
        self._write(workspace_id, value)
        return self._view(value)


def _derive_states(value: dict[str, Any]) -> dict[str, bool]:
    """The five milestones backed by this workspace's mapping/import records.

    Knowledge documents and effective rules are independent resources. Their
    tenant-wide availability is not evidence that this workspace published a
    document or activated a rule.
    """
    block = value.get("import") or {}
    execution = block.get("execution") or {}
    readback = block.get("readback") or {}
    return {
        "saved": True,
        "mapping_confirmed": bool(value.get("mapping_adoption")),
        "context_adopted": bool(value.get("mapping_adoption")),
        "import_executed": bool(execution.get("executed_at")),
        "import_readback_passed": bool(readback.get("passed")),
    }


def mapping_targets() -> dict[str, Any]:
    return {
        "models": [
            {"model": model, "label": _MODEL_LABELS[model], "fields": list(fields)}
            for model, fields in TARGET_MODELS.items()
        ]
    }

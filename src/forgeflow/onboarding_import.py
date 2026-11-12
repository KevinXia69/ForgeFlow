"""Preview, authorize, import and read back a confirmed onboarding mapping.

The onboarding workspace used to stop at "mapping confirmed" and call the
mapping summary ``applied``.  That word implied ERP data existed when nothing had
been written.  This module is the missing second half:

    confirmed mapping -> import preview -> explicit authorization
    -> idempotent create/update -> readback

It is deliberately bounded to allowlisted record types with fixed writable fields.
Anything else is refused instead of being silently ignored or written through a
generic CRUD surface.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Protocol

#: The record types this importer may write.
WRITABLE_FIELDS: dict[str, frozenset[str]] = {
    "res.partner": frozenset({
        "name", "ref", "email", "phone", "website", "vat", "is_company",
        "supplier_rank", "customer_rank", "comment",
    }),
    "product.product": frozenset({
        "name", "default_code", "list_price", "standard_price", "barcode", "type",
        "uom_id",
    }),
    "uom.uom": frozenset({"name", "factor"}),
    "stock.warehouse": frozenset({"name", "code"}),
    "mrp.workcenter": frozenset({"name", "code", "capacity", "time_efficiency"}),
    "purchase.order": frozenset({"name", "partner_id", "date_order", "partner_ref",
                                 "date_planned", "origin", "company_id"}),
    "sale.order": frozenset({"name", "partner_id", "date_order", "client_order_ref",
                             "commitment_date", "origin", "company_id"}),
    "mrp.bom": frozenset({"product_tmpl_id", "product_qty", "type", "code",
                          "company_id"}),
    "purchase.order.line": frozenset({"order_id", "product_id", "product_qty",
                                      "price_unit", "date_planned", "name"}),
    "sale.order.line": frozenset({"order_id", "product_id", "product_uom_qty",
                                  "price_unit", "customer_lead", "name"}),
    "mrp.bom.line": frozenset({"bom_id", "product_id", "product_qty"}),
}

#: Identity fields, in priority order.  An external id is preferred over a name.
IDENTITY_FIELDS: dict[str, tuple[str, ...]] = {
    "res.partner": ("ref", "name"),
    "product.product": ("default_code", "name"),
    "uom.uom": ("name",),
    "stock.warehouse": ("code", "name"),
    "mrp.workcenter": ("code", "name"),
    "purchase.order": ("partner_ref", "name"),
    "sale.order": ("client_order_ref", "name"),
    "mrp.bom": ("code",),
}

#: Models whose identity is a tuple of fields, not one source column.
COMPOSITE_IDENTITY: dict[str, tuple[str, ...]] = {
    "purchase.order.line": ("order_id", "product_id"),
    "sale.order.line": ("order_id", "product_id"),
    "mrp.bom.line": ("bom_id", "product_id"),
}

#: ``(model, target field)`` -> ERP model and lookup columns.
#: Parent relations that may point at a record created earlier in the same run.
DEFERRED_RELATIONS = {
    ("purchase.order.line", "order_id"),
    ("sale.order.line", "order_id"),
    ("mrp.bom.line", "bom_id"),
}

RELATION_FIELDS: dict[tuple[str, str], tuple[str, tuple[str, ...]]] = {
    ("purchase.order", "partner_id"): ("res.partner", ("ref", "name")),
    ("purchase.order", "company_id"): ("res.company", ("name",)),
    ("sale.order", "partner_id"): ("res.partner", ("ref", "name")),
    ("sale.order", "company_id"): ("res.company", ("name",)),
    ("product.product", "uom_id"): ("uom.uom", ("name",)),
    ("mrp.bom", "product_tmpl_id"): ("product.product", ("default_code", "name")),
    ("mrp.bom", "company_id"): ("res.company", ("name",)),
    ("purchase.order.line", "order_id"): ("purchase.order", ("partner_ref", "name")),
    ("purchase.order.line", "product_id"): ("product.product", ("default_code", "name")),
    ("sale.order.line", "order_id"): ("sale.order", ("client_order_ref", "name")),
    ("sale.order.line", "product_id"): ("product.product", ("default_code", "name")),
    ("mrp.bom.line", "bom_id"): ("mrp.bom", ("code",)),
    ("mrp.bom.line", "product_id"): ("product.product", ("default_code", "name")),
}

REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "res.partner": ("name",),
    "product.product": ("name",),
    "uom.uom": ("name",),
    "stock.warehouse": ("name", "code"),
    "mrp.workcenter": ("name",),
    "purchase.order": ("partner_id",),
    "sale.order": ("partner_id",),
    "mrp.bom": ("product_tmpl_id", "product_qty"),
    "purchase.order.line": ("order_id", "product_id", "product_qty"),
    "sale.order.line": ("order_id", "product_id", "product_uom_qty"),
    "mrp.bom.line": ("bom_id", "product_id", "product_qty"),
}

NUMERIC_FIELDS = frozenset({"supplier_rank", "customer_rank", "list_price",
                            "standard_price", "factor", "rounding", "capacity",
                            "time_efficiency", "product_qty", "product_uom_qty",
                            "price_unit"})
BOOLEAN_FIELDS = frozenset({"is_company"})


class ImportError(ValueError):
    """A bounded, client-safe import refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ImportAdapter(Protocol):
    def search_read(self, model: str, domain: list, fields: list[str],
                    limit: int = 100) -> list[dict]: ...

    def create(self, model: str, values: dict) -> int: ...

    def write(self, model: str, ids: list[int], values: dict) -> bool: ...


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def build_preview(workspace: dict[str, Any], records: dict[str, dict[str, Any]],
                  adapter: ImportAdapter) -> dict[str, Any]:
    """Compare mapped source rows with the ERP, without writing anything.

    ``records`` maps a source id to ``{"headers": [...], "rows": [[...], ...]}``.
    Every source must map to exactly one model: a row that would create a
    partner *and* a product from the same line has no single identity.
    """
    mappings = [row for row in workspace.get("mappings", [])
                if row.get("status") == "confirmed"]
    if not mappings:
        raise ImportError("mapping_not_confirmed")
    by_source: dict[str, list[dict]] = {}
    for mapping in mappings:
        by_source.setdefault(str(mapping["source_id"]), []).append(mapping)
    rows: list[dict[str, Any]] = []
    blocking: list[dict[str, str]] = []
    seen: set[tuple[str, str | None, str]] = set()
    for source_id, source_mappings in sorted(by_source.items()):
        models = {str(row["target_model"]) for row in source_mappings}
        if len(models) != 1:
            blocking.append({"code": "source_spans_models", "source_id": source_id})
            continue
        model = next(iter(models))
        if model not in WRITABLE_FIELDS:
            blocking.append({"code": "model_not_importable", "source_id": source_id})
            continue
        for mapping in source_mappings:
            if str(mapping["target_field"]) not in WRITABLE_FIELDS[model]:
                blocking.append({"code": "field_not_writable",
                                 "source_id": source_id,
                                 "field": str(mapping["target_field"])})
        source_records = records.get(source_id) or {}
        headers = list(source_records.get("headers") or [])
        positions = {name: index for index, name in enumerate(headers)}
        for number, raw in enumerate(source_records.get("rows") or [], start=1):
            values: dict[str, Any] = {}
            deferred: dict[str, Any] = {}
            problems: list[str] = []
            for mapping in source_mappings:
                header = str(mapping["source_field"])
                index = positions.get(header)
                if index is None or index >= len(raw):
                    problems.append("source_value_missing")
                    continue
                field = str(mapping["target_field"])
                parsed, error = _value(model, field, raw[index], adapter)
                if (error and (model, field) in DEFERRED_RELATIONS
                        and error.startswith("relation_not_found")):
                    deferred[field] = raw[index]
                elif error:
                    problems.append(error)
                else:
                    values[field] = parsed
            if hasattr(adapter, "prepare_values"):
                try:
                    values = adapter.prepare_values(model, values)
                except ImportError as error:
                    problems.append(error.code)
            row = _preview_row(model, source_id, number, values, problems, adapter,
                               deferred=deferred)
            if row["action"] in ("create", "update") and hasattr(adapter, "validate_write"):
                try:
                    adapter.validate_write(model, row.get("existing_id"), values)
                except ImportError as error:
                    row.update(action="conflict", existing_id=None, changes={},
                               problems=[*row["problems"], error.code])
            if row["action"] != "conflict" and row["identity"] is not None:
                key = (row["model"], row["identity_field"], str(row["identity"]))
                if key in seen:
                    row["problems"] = [*row["problems"], "duplicate_identity"]
                    row.update(action="conflict", existing_id=None, changes={})
                else:
                    seen.add(key)
            rows.append(row)
    rows = _dependency_ordered(rows)
    counts = {"create": 0, "update": 0, "unchanged": 0, "conflict": 0}
    for row in rows:
        counts[row["action"]] = counts.get(row["action"], 0) + 1
    preview = {
        "schema_version": 1,
        "workspace_id": workspace.get("id"),
        "mapping_sha256": (workspace.get("mapping_adoption") or {}).get(
            "mapping_sha256"),
        "rows": rows,
        "counts": counts,
        "blocking": blocking,
        "importable": not blocking and all(row["action"] != "conflict" for row in rows),
    }
    if getattr(adapter, "access_scope", None) is not None:
        preview["access_scope"] = adapter.access_scope
    preview["preview_sha256"] = _digest(preview)
    return preview


def _dependency_ordered(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Stable topological order: parents before lines that defer to them."""
    by_identity: dict[tuple[str, str, str], str] = {}
    for row in rows:
        for field, value in (row.get("values") or {}).items():
            if value not in (None, ""):
                by_identity.setdefault(
                    (row["model"], field, str(value)), str(row["key"]))
    dependencies: dict[str, str] = {}
    for row in rows:
        for field, raw in (row.get("deferred_relations") or {}).items():
            spec = RELATION_FIELDS.get((row["model"], field))
            if spec is None:
                continue
            parent_model, lookups = spec
            for lookup in lookups:
                key = by_identity.get((parent_model, lookup, str(raw)))
                if key is not None:
                    dependencies[str(row["key"])] = key
                    break
    ordered: list[dict[str, Any]] = []
    remaining = {str(row["key"]): row for row in rows}
    emitted: set[str] = set()
    while remaining:
        ready = [key for key in remaining
                 if dependencies.get(key) is None or dependencies[key] in emitted]
        if not ready:
            # A cycle is not silently reordered; leave the original order so the
            # execution failure is explicit.
            ordered.extend(remaining.values())
            break
        for key in ready:
            ordered.append(remaining.pop(key))
            emitted.add(key)
    return ordered


def _preview_row(model: str, source_id: str, number: int, values: dict[str, Any],
                 problems: list[str], adapter: ImportAdapter,
                 *, deferred: dict[str, Any] | None = None) -> dict[str, Any]:
    deferred = deferred or {}
    composite = COMPOSITE_IDENTITY.get(model)
    if composite:
        identity_field = ",".join(composite)
        missing_identity = [field for field in composite
                            if values.get(field) in (None, "")
                            and field not in deferred]
        identity = tuple(
            values.get(field) if values.get(field) not in (None, "")
            else deferred.get(field) for field in composite)
        identity_domain = [[field, "=", values.get(field)] for field in composite]
    else:
        identity_field = next((field for field in IDENTITY_FIELDS[model]
                               if values.get(field) not in (None, "")), None)
        missing_identity = []
        identity = values.get(identity_field) if identity_field else None
        identity_domain = ([[identity_field, "=", identity]]
                           if identity_field is not None else [])
    missing = [field for field in REQUIRED_FIELDS[model]
               if values.get(field) in (None, "") and field not in deferred]
    if missing_identity:
        problems.append("identity_field_missing:" + ",".join(missing_identity))
    if missing:
        problems.append("required_field_missing:" + ",".join(missing))
    row = {
        "key": f"{source_id}:{number}",
        "source_id": source_id,
        "row": number,
        "model": model,
        "identity_field": identity_field,
        "identity": identity,
        "identity_domain": identity_domain,
        "deferred_relations": deferred,
        "values": values,
        "problems": problems,
    }
    if problems:
        row.update(action="conflict", existing_id=None, changes={})
        return row
    if deferred:
        # The parent does not exist yet; it is created earlier in this run.
        row.update(action="create", existing_id=None, changes=values)
        return row
    matches = adapter.search_read(
        model, identity_domain,
        ["id", *sorted(set(values) | set(REQUIRED_FIELDS[model]))], limit=2)
    if len(matches) > 1:
        row.update(action="conflict", existing_id=None, changes={},
                   problems=["ambiguous_existing"])
        return row
    if not matches:
        row.update(action="create", existing_id=None, changes=values)
        return row
    existing = matches[0]
    changes = {field: value for field, value in values.items()
               if not _same_value(existing.get(field), value)}
    row.update(action="update" if changes else "unchanged",
               existing_id=int(existing["id"]), changes=changes,
               before={field: existing.get(field) for field in values})
    return row


def _same_value(live: Any, expected: Any) -> bool:
    if isinstance(live, (list, tuple)):
        live = live[0] if live else None
    return live == expected


def _resolve_relation(adapter: ImportAdapter, model: str, field: str,
                      raw: Any) -> tuple[Any, str | None]:
    spec = RELATION_FIELDS.get((model, field))
    if spec is None:
        return raw, None
    target, lookups = spec
    text = "" if raw is None else str(raw).strip()
    if not text:
        return None, f"relation_missing:{field}"
    matches: list[dict[str, Any]] = []
    if text.isdigit():
        direct = adapter.search_read(target, [("id", "=", int(text))],
                                     ["id"], limit=1)
        if direct:
            matches = direct
    for lookup in ([] if matches else lookups):
        matches = adapter.search_read(target, [(lookup, "=", text)], ["id"], limit=2)
        if matches:
            break
    if not matches:
        return None, f"relation_not_found:{field}"
    if len(matches) > 1:
        return None, f"relation_ambiguous:{field}"
    identifier = int(matches[0]["id"])
    if field == "product_tmpl_id":
        rows = adapter.search_read("product.product", [("id", "=", identifier)],
                                   ["product_tmpl_id"], limit=1)
        nested = rows[0].get("product_tmpl_id") if rows else None
        nested_id = nested[0] if isinstance(nested, (list, tuple)) else nested
        if not nested_id:
            return None, "relation_not_found:product_tmpl_id"
        return int(nested_id), None
    return identifier, None


def _value(model: str, field: str, raw: Any,
           adapter: ImportAdapter) -> tuple[Any, str | None]:
    if (model, field) in RELATION_FIELDS:
        return _resolve_relation(adapter, model, field, raw)
    text = "" if raw is None else str(raw).strip()
    if field in NUMERIC_FIELDS:
        try:
            number = float(text)
        except ValueError:
            return None, f"value_not_numeric:{field}"
        return (int(number) if number.is_integer() else number), None
    if field in BOOLEAN_FIELDS:
        lowered = text.casefold()
        if lowered in {"1", "true", "yes", "y"}:
            return True, None
        if lowered in {"0", "false", "no", "n", ""}:
            return False, None
        return None, f"value_not_boolean:{field}"
    return text, None


def authorize(preview: dict[str, Any], *, uid: str, preview_sha256: str,
              now: str) -> dict[str, Any]:
    if preview_sha256 != preview.get("preview_sha256"):
        raise ImportError("import_preview_changed")
    if not preview.get("importable"):
        raise ImportError("import_preview_blocked")
    return {"by": uid, "at": now, "preview_sha256": preview_sha256}


def execute(preview: dict[str, Any], *, authorized: dict[str, Any],
            adapter: ImportAdapter, previous: list[dict] | None = None,
            now: str = "") -> dict[str, Any]:
    """Apply the preview idempotently, keeping per-row results across retries."""
    if authorized.get("preview_sha256") != preview.get("preview_sha256"):
        raise ImportError("import_not_authorized")
    if not preview.get("importable"):
        raise ImportError("import_preview_blocked")
    done = {str(row["key"]): row for row in (previous or [])
            if row.get("status") == "succeeded"}
    results: list[dict[str, Any]] = []
    for row in preview.get("rows") or []:
        key = str(row["key"])
        if key in done:
            results.append(done[key])
            continue
        results.append(_apply_row(row, adapter))
    counts = {"succeeded": 0, "failed": 0, "unchanged": 0}
    for row in results:
        counts[row["status"]] = counts.get(row["status"], 0) + 1
    return {"preview_sha256": preview["preview_sha256"], "rows": results,
            "counts": counts, "executed_at": now,
            "status": "imported" if counts["failed"] == 0 else "import_partial"}


def _apply_row(row: dict[str, Any], adapter: ImportAdapter) -> dict[str, Any]:
    base = {"key": row["key"], "model": row["model"],
            "identity_field": row["identity_field"], "identity": row["identity"]}
    try:
        values = dict(row["values"])
        for field, raw in (row.get("deferred_relations") or {}).items():
            resolved, error = _resolve_relation(adapter, row["model"], field, raw)
            if error:
                return {**base, "status": "failed", "error": error}
            values[field] = resolved
        if row["action"] == "create":
            identifier = int(adapter.create(row["model"], values))
            return {**base, "status": "succeeded", "operation": "create",
                    "record_id": identifier}
        if row["action"] == "update":
            adapter.write(row["model"], [int(row["existing_id"])], row["changes"])
            return {**base, "status": "succeeded", "operation": "update",
                    "record_id": int(row["existing_id"])}
        return {**base, "status": "unchanged", "operation": "unchanged",
                "record_id": int(row["existing_id"])}
    except Exception as error:  # noqa: BLE001 - one row must not stop the import
        return {**base, "status": "failed",
                "error": _reason(error)}


def _reason(error: Exception) -> str:
    text = str(error).strip()
    if text and len(text) <= 80 and " " not in text:
        return text
    return "import_row_failed"


def readback(preview: dict[str, Any], execution: dict[str, Any],
             adapter: ImportAdapter) -> dict[str, Any]:
    """Read each imported record back by its identity and compare the fields."""
    executed = {str(row.get("key")): row for row in execution.get("rows") or []}
    rows: list[dict[str, Any]] = []
    # The preview is the expected set: an execution row that is missing or
    # failed means the import is not complete, even when every other row matches.
    for expected in preview.get("rows") or []:
        key = str(expected["key"])
        result = executed.get(key)
        if result is None:
            rows.append({"key": key, "model": expected["model"],
                         "identity_field": expected["identity_field"],
                         "identity": expected["identity"],
                         "readback": "not_executed"})
            continue
        if result.get("status") not in ("succeeded", "unchanged"):
            rows.append({**result, "readback": "failed"})
            continue
        if result.get("record_id"):
            matches = adapter.search_read(
                result["model"], [("id", "=", int(result["record_id"]))],
                ["id", *sorted(expected["values"])], limit=2)
        else:
            domain = expected.get("identity_domain") or [
                [result["identity_field"], "=", result["identity"]]]
            matches = adapter.search_read(
                result["model"], [tuple(part) for part in domain],
                ["id", *sorted(expected["values"])], limit=2)
        if len(matches) != 1:
            rows.append({**result, "readback": "missing" if not matches
                         else "ambiguous"})
            continue
        live = matches[0]
        mismatched = [field for field, value in expected["values"].items()
                      if not _same_value(live.get(field), value)]
        rows.append({**result, "readback": "matched" if not mismatched
                     else "mismatched", "mismatched": mismatched})
    passed = bool(rows) and all(row.get("readback") == "matched" for row in rows)
    return {"rows": rows, "passed": passed,
            "status": "readback_passed" if passed else "readback_failed"}

"""Small read-only investigation sources; no answer files or general ORM access.

Both sources expose ``query(kind, arguments)`` and return exactly
``{"records": [...], "complete": bool, "next_cursor": str | None}``.
Every record is ``{"entity": str, "id": str, "fields": dict}``. Business IDs
and relation IDs are positive decimal strings, never display names. The fields
are deliberately stable so evidence can refer to an individual field:

* supplier: name, ref
* product: name, default_code, product_template_id
* purchase_order: name, state, supplier_id, origin
* sales_order: name, state, customer_id, commitment_date, client_order_ref
* purchase_order_line: order_id, product_id, quantity, unit_price,
  received_quantity, planned_date
* sales_order_line: order_id, product_id, quantity, unit_price, delivered_quantity
* supplier_offer: supplier_id, supplier_ref, product_id, product_template_id,
  product_code, unit_price, lead_days, minimum_quantity, offer_key
* bom: product_id (nullable), product_template_id, quantity, type, active
* bom_line: bom_id, product_id, quantity
* policy: task_type, anchor_date, margin, margin_basis, time_granularity
* order_policy: authorized, budget_cap

``policy`` IDs are contract IDs. ``order_policy`` IDs are sales-order IDs.
Dates are strings or null for absent business dates. ``budget_cap`` is a finite
positive number or null. ``authorized`` is a boolean, independently of whether
a budget is known. An offer for a template with multiple product variants has
null product_id/product_code/offer_key; the source never picks a variant.

``entities`` accepts one of the four main entities plus a nonempty query,
limit (1..50, default 20), and an optional cursor. ``relations`` accepts a main
entity or bom and its ID, plus the same pagination arguments. ``record`` accepts any
of the above business entities, or policy/order_policy, and an ID. ``policy``
accepts an optional order_id. It never enumerates the authorized order set.

Relations: supplier -> purchase orders, offers; product -> sales lines,
purchase lines, offers, matching active BoMs, and lines using this component;
bom -> lines and parent products; purchase order -> supplier, purchase lines,
sales orders named by its comma-separated origin; sales order -> sales lines.
Template BoMs retain a null product_id and return every matching parent variant;
the source never selects a variant or infers a production requirement.
Product relations include ALL non-display sales lines, without silently
filtering order state or delivery. Callers must read the parent sales order.
The planner considers unselected demand active when the order is sale/done and
delivered_quantity < quantity; selecting an order has stricter requirements.

Odoo only receives fixed fields_get/search/read calls and scoped domains. The
fixture source takes normalized records and either a TaskContract or the
following explicit policy shape (no hidden reference/request is accepted):
``{contract_id, task_type, anchor_date, margin, margin_basis, time_granularity,
orders: {order_id: {authorized: bool, budget_cap: number | null}}}``.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import re
from datetime import date, datetime
from typing import Any

from forgeflow.contract import TaskContract
from forgeflow.investigation.transport import PAGE_BYTES, page_size

MAX_LIMIT = 50
MAX_ID = 2**53 - 1
MAX_VIEW_RECORDS = 2000
MAX_VIEW_BYTES = 16 * 1024 * 1024
MAX_SCOPE_RECORDS = 1000
MAIN_ENTITIES = frozenset({"supplier", "product", "purchase_order", "sales_order"})
RELATION_ENTITIES = MAIN_ENTITIES | {"bom"}
FIELDS = {
    "supplier": ("name", "ref"),
    "product": ("name", "default_code", "product_template_id"),
    "purchase_order": ("name", "state", "supplier_id", "origin"),
    "sales_order": ("name", "state", "customer_id", "commitment_date", "client_order_ref"),
    "purchase_order_line": (
        "order_id", "product_id", "quantity", "unit_price", "received_quantity", "planned_date",
    ),
    "sales_order_line": (
        "order_id", "product_id", "quantity", "unit_price", "delivered_quantity",
    ),
    "supplier_offer": (
        "supplier_id", "supplier_ref", "product_id", "product_template_id", "product_code",
        "unit_price", "lead_days", "minimum_quantity", "offer_key",
    ),
    "bom": ("product_id", "product_template_id", "quantity", "type", "active"),
    "bom_line": ("bom_id", "product_id", "quantity"),
}
MODELS = {
    "supplier": "res.partner", "product": "product.product",
    "purchase_order": "purchase.order", "sales_order": "sale.order",
    "purchase_order_line": "purchase.order.line", "sales_order_line": "sale.order.line",
    "supplier_offer": "product.supplierinfo",
    "bom": "mrp.bom", "bom_line": "mrp.bom.line",
}
RAW_FIELDS = {
    "supplier": ("id", "name", "ref", "supplier_rank"),
    "product": ("id", "name", "default_code", "product_tmpl_id"),
    "purchase_order": ("id", "name", "state", "partner_id", "origin"),
    "sales_order": ("id", "name", "state", "partner_id", "commitment_date", "client_order_ref"),
    "purchase_order_line": (
        "id", "order_id", "product_id", "product_qty", "price_unit", "qty_received",
        "date_planned", "display_type",
    ),
    "sales_order_line": (
        "id", "order_id", "product_id", "product_uom_qty", "price_unit", "qty_delivered",
        "display_type",
    ),
    "supplier_offer": (
        "id", "partner_id", "product_id", "product_tmpl_id", "price", "delay", "min_qty",
    ),
    "bom": ("id", "product_id", "product_tmpl_id", "product_qty", "type", "active"),
    "bom_line": ("id", "bom_id", "product_id", "product_qty"),
}
RAW_NAMES = {
    "product_template_id": "product_tmpl_id", "supplier_id": "partner_id",
    "customer_id": "partner_id",
}
POLICY_FIELDS = ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")


class SourceError(ValueError):
    """A closed, non-sensitive source failure suitable for a tool adapter."""

    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


SOURCE_ERROR_CODES = frozenset({
    "invalid_source_arguments", "numeric_record_id_required", "invalid_source_record",
    "invalid_source_response", "invalid_source_policy", "source_schema_unavailable",
    "source_relation_missing", "source_relation_limit", "source_changed_during_read",
    "source_view_changed", "source_view_limit", "source_record_too_large",
    "query_response_too_large", "query_operation_conflict", "query_receipt_unavailable",
})


def source_error_feedback(code):
    reason = code if isinstance(code, str) and code in SOURCE_ERROR_CODES else "investigation_query_rejected"
    result = {"status": "tool_error", "code": "invalid_field", "reason": reason}
    if reason == "numeric_record_id_required":
        result["hint"] = ("Use the numeric id returned by records for id/order_id, never a business "
                          "name such as SO-1 or PO-20. Search a name with kind=entities, query=<name> first.")
    elif reason == "invalid_source_arguments":
        result["hint"] = ("Check the query kind and its arguments: entities needs entity/query; "
                          "record or relations needs entity/id; policy accepts only optional order_id.")
    elif reason == "source_view_changed":
        result["hint"] = "Discard this traversal and restart without a cursor; business records changed between pages."
    elif reason in {"source_view_limit", "source_record_too_large", "query_response_too_large"}:
        result["hint"] = "This bounded read cannot be delivered safely. Narrow the entity query or stop for clarification; no fields were truncated."
    return result


def _need(condition: bool, code: str = "invalid_source_arguments") -> None:
    if not condition:
        raise SourceError(code)


def collect_pages(source, kind, arguments, *, maximum=MAX_SCOPE_RECORDS):
    """Keep the prior 1,000-record scope bound when byte pages become smaller."""
    records, cursor, seen = [], None, set()
    for _ in range(maximum + 1):
        args = {**arguments, "limit": MAX_LIMIT}
        if cursor is not None:
            args["cursor"] = cursor
        page = source.query(kind, args)
        records.extend(page["records"])
        if len(records) > maximum:
            break
        if page["complete"]:
            return records
        cursor = page.get("next_cursor")
        if not page["records"] or not isinstance(cursor, str) or cursor in seen:
            break
        seen.add(cursor)
    raise ValueError("scope_query_incomplete")


def _id(value: Any, *, nullable: bool = False) -> str | None:
    if nullable and (value is None or value is False):
        return None
    if isinstance(value, (list, tuple)):
        _need(len(value) == 2, "invalid_source_record")
        value = value[0]
    if type(value) is int:
        _need(0 < value <= MAX_ID, "invalid_source_record")
        return str(value)
    _need(isinstance(value, str) and bool(re.fullmatch(r"[1-9][0-9]{0,15}", value)),
          "numeric_record_id_required")
    _need(int(value) <= MAX_ID, "numeric_record_id_required")
    return value


def _text(value: Any, *, maximum: int = 512, optional: bool = False) -> str:
    if optional and (value is False or value is None):
        return ""
    _need(isinstance(value, str) and len(value) <= maximum, "invalid_source_record")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise SourceError("invalid_source_record") from None
    return value


def _number(value: Any, *, positive: bool = False) -> int | float:
    _need(type(value) in (int, float) and math.isfinite(value)
          and (value > 0 if positive else value >= 0), "invalid_source_record")
    return value


def _date(value: Any) -> str | None:
    if value is None or value is False:
        return None
    value = _text(value, maximum=64)
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SourceError("invalid_source_record") from None
    return value


def _record(entity: str, identity: Any, fields: dict) -> dict:
    return {"entity": entity, "id": _id(identity), "fields": fields}


def _normalized(record: Any) -> dict:
    _need(isinstance(record, dict) and set(record) == {"entity", "id", "fields"},
          "invalid_source_record")
    entity, fields = record["entity"], record["fields"]
    _need(isinstance(entity, str) and entity in FIELDS and isinstance(fields, dict)
          and set(fields) == set(FIELDS[entity]), "invalid_source_record")
    cleaned = {}
    for key, value in fields.items():
        if key.endswith("_id"):
            cleaned[key] = _id(value, nullable=entity in {"supplier_offer", "bom"} and key == "product_id")
        elif key == "active":
            _need(type(value) is bool, "invalid_source_record")
            cleaned[key] = value
        elif key in {"quantity", "unit_price", "received_quantity", "delivered_quantity",
                     "lead_days", "minimum_quantity"}:
            cleaned[key] = _number(value, positive=entity in {"bom", "bom_line"})
            if key in {"lead_days", "minimum_quantity"}:
                _need(float(value).is_integer(), "invalid_source_record")
        elif key in {"commitment_date", "planned_date"}:
            cleaned[key] = _date(value)
        elif entity == "supplier_offer" and key in {"product_code", "offer_key"} and value is None:
            cleaned[key] = None
        else:
            cleaned[key] = _text(value, maximum=4096 if key == "origin" else 512)
    return _record(entity, record["id"], cleaned)


def _policy(value: TaskContract | dict) -> dict:
    if isinstance(value, TaskContract):
        return {
            "contract_id": value.contract_id,
            **{field: getattr(value, field) for field in POLICY_FIELDS},
            "orders": {str(order): {"authorized": True, "budget_cap": value.budgets[str(order)]}
                       for order in value.sales_order_ids},
        }
    _need(isinstance(value, dict)
          and set(value) == {"contract_id", "orders", *POLICY_FIELDS}, "invalid_source_policy")
    result = copy.deepcopy(value)
    _need(isinstance(result["contract_id"], str)
          and bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", result["contract_id"])),
          "invalid_source_policy")
    _need(result["task_type"] in {"supplier_cancellation", "workcenter_outage"}
          and result["time_granularity"] == "day", "invalid_source_policy")
    try:
        _need(date.fromisoformat(result["anchor_date"]).isoformat() == result["anchor_date"],
              "invalid_source_policy")
        _need(0 < _number(result["margin"]) < 1, "invalid_source_policy")
        _text(result["margin_basis"])
    except (TypeError, ValueError):
        raise SourceError("invalid_source_policy") from None
    _need(isinstance(result["orders"], dict), "invalid_source_policy")
    for key, entry in result["orders"].items():
        _id(key)
        _need(isinstance(entry, dict) and set(entry) == {"authorized", "budget_cap"}
              and type(entry["authorized"]) is bool, "invalid_source_policy")
        if entry["budget_cap"] is not None:
            _number(entry["budget_cap"], positive=True)
        _need(entry["authorized"] or entry["budget_cap"] is None, "invalid_source_policy")
    return result


class _Source:
    def __init__(self, policy: TaskContract | dict):
        self._policy = _policy(policy)

    def _search(self, entity: str, domain: list, limit: int, offset: int) -> list[dict]:
        raise NotImplementedError

    def _get(self, entity: str, identity: str) -> dict | None:
        rows = self._search(entity, [("id", "=", identity)], 1, 0)
        return rows[0] if rows else None

    def _policy_record(self, order_id: str | None = None) -> dict:
        if order_id is None:
            return {"entity": "policy", "id": self._policy["contract_id"],
                    "fields": {field: self._policy[field] for field in POLICY_FIELDS}}
        return {"entity": "order_policy", "id": order_id,
                "fields": copy.deepcopy(self._policy["orders"].get(
                    order_id, {"authorized": False, "budget_cap": None}))}

    def _routes(self, entity: str, identity: str) -> list[tuple[str, list]]:
        row = self._get(entity, identity)
        if row is None:
            return []
        fields = row["fields"]
        if entity == "supplier":
            return [("purchase_order", [("supplier_id", "=", identity)]),
                    ("supplier_offer", [("supplier_id", "=", identity)])]
        if entity == "product":
            return [("sales_order_line", [("product_id", "=", identity)]),
                    ("purchase_order_line", [("product_id", "=", identity)]),
                    ("supplier_offer", [("product_template_id", "=", fields["product_template_id"]),
                                        ("product_id", "in", [None, identity])]),
                    ("bom", [("product_template_id", "=", fields["product_template_id"]),
                             ("product_id", "in", [None, identity]), ("active", "=", True)]),
                    ("bom_line", [("product_id", "=", identity)])]
        if entity == "bom":
            parent = ([("id", "=", fields["product_id"])] if fields["product_id"] else
                      [("product_template_id", "=", fields["product_template_id"])])
            return [("bom_line", [("bom_id", "=", identity)]), ("product", parent)]
        if entity == "sales_order":
            return [("sales_order_line", [("order_id", "=", identity)])]
        origins = [token.strip() for token in fields["origin"].split(",") if token.strip()]
        _need(len(origins) <= 50, "source_relation_limit")
        routes = [("supplier", [("id", "=", fields["supplier_id"])]),
                  ("purchase_order_line", [("order_id", "=", identity)])]
        if origins:
            routes.append(("sales_order", [("name", "in", origins)]))
        return routes

    @staticmethod
    def _result(rows: list[dict], cursor: str | None = None) -> dict:
        _need(page_size(rows, cursor) <= PAGE_BYTES, "source_record_too_large")
        return {"records": copy.deepcopy(rows), "complete": cursor is None, "next_cursor": cursor}

    def _view(self, routes):
        """Read only this scoped, ordered view, with explicit resource bounds.

        A view is not an ERP transaction snapshot. Its digest detects any changed
        returned record/route on continuation; preparation independently rereads
        the facts. No silently truncated view is eligible for a complete page.
        """
        rows, size = [], 0
        for entity, domain in routes:
            offset = 0
            while True:
                chunk = self._search(entity, domain, MAX_LIMIT, offset)
                _need(isinstance(chunk, list) and len(chunk) <= MAX_LIMIT, "invalid_source_response")
                for row in chunk:
                    size += len(json.dumps(row, ensure_ascii=True, allow_nan=False).encode())
                    _need(len(rows) < MAX_VIEW_RECORDS and size <= MAX_VIEW_BYTES, "source_view_limit")
                    rows.append(row)
                if len(chunk) < MAX_LIMIT:
                    break
                offset += len(chunk)
        identities = [(row["entity"], row["id"]) for row in rows]
        _need(len(set(identities)) == len(identities), "source_changed_during_read")
        return rows

    def query(self, kind: str, arguments: dict) -> dict:
        _need(type(kind) is str and isinstance(arguments, dict))
        args = dict(arguments)
        if kind == "policy":
            _need(set(args) <= {"order_id"})
            identity = _id(args["order_id"]) if "order_id" in args else None
            return self._result([self._policy_record(identity)])
        if kind == "record":
            _need(set(args) == {"entity", "id"})
            entity = args["entity"]
            _need(isinstance(entity, str) and entity in {*FIELDS, "policy", "order_policy"})
            if entity == "policy":
                _need(isinstance(args["id"], str))
                rows = [self._policy_record()] if args["id"] == self._policy["contract_id"] else []
            elif entity == "order_policy":
                rows = [self._policy_record(_id(args["id"]))]
            else:
                row = self._get(entity, _id(args["id"]))
                rows = [row] if row else []
            return self._result(rows)
        _need(kind in {"entities", "relations"})
        key = "query" if kind == "entities" else "id"
        _need({"entity", key} <= set(args) <= {"entity", key, "limit", "cursor"})
        entity = args["entity"]
        allowed = MAIN_ENTITIES if kind == "entities" else RELATION_ENTITIES
        _need(isinstance(entity, str) and entity in allowed)
        limit = args.get("limit", 20)
        _need(type(limit) is int and 1 <= limit <= MAX_LIMIT)
        if kind == "entities":
            value = args["query"]
            _need(isinstance(value, str) and 0 < len(value.strip()) <= 128)
            value = value.strip()
            routes = [(entity, [("__query__", "=", value)])]
        else:
            value = _id(args["id"])
            routes = None  # Validate the cursor before making any source calls.
        fingerprint = hashlib.sha256(json.dumps([kind, entity, value]).encode()).hexdigest()[:16]
        offset = 0
        previous_view = None
        cursor = args.get("cursor")
        if cursor is not None:
            _need(isinstance(cursor, str) and len(cursor) <= 100)
            match = re.fullmatch(r"v2:([0-9a-f]{16}):([0-9a-f]{64}):([0-9]{1,4})", cursor)
            _need(match is not None and match[1] == fingerprint)
            previous_view, offset = match[2], int(match[3])
            _need(0 < offset <= MAX_VIEW_RECORDS)
        if routes is None:
            routes = self._routes(entity, value)
        rows = self._view(routes)
        view = hashlib.sha256(json.dumps([routes, rows], ensure_ascii=True,
                                        sort_keys=True, allow_nan=False).encode()).hexdigest()
        _need(previous_view is None or previous_view == view, "source_view_changed")
        _need(offset <= len(rows))
        output = []
        next_cursor = None
        for row in rows[offset:offset + limit]:
            position = offset + len(output) + 1
            candidate_cursor = f"v2:{fingerprint}:{view}:{position}" if position < len(rows) else None
            if page_size([*output, row], candidate_cursor) > PAGE_BYTES:
                _need(bool(output), "source_record_too_large")
                break
            output.append(row)
            next_cursor = candidate_cursor
        return self._result(output, next_cursor)


class FixtureSource(_Source):
    """A fixed table of normalized business records, with no file-loading API."""

    def __init__(self, records: list[dict], policy: TaskContract | dict):
        super().__init__(policy)
        _need(isinstance(records, list) and len(records) <= 10_000, "invalid_source_record")
        self.records = [_normalized(record) for record in records]
        identities = [(record["entity"], record["id"]) for record in self.records]
        _need(len(set(identities)) == len(identities), "invalid_source_record")

    def _search(self, entity: str, domain: list, limit: int, offset: int) -> list[dict]:
        def matches(row: dict) -> bool:
            for field, op, expected in domain:
                if field == "__query__":
                    names = ("name", "ref") if entity == "supplier" else (
                        ("name", "default_code") if entity == "product" else ("name",))
                    if not any(expected.casefold() in row["fields"][name].casefold()
                               for name in names) and row["id"] != expected:
                        return False
                    continue
                actual = row["id"] if field == "id" else row["fields"][field]
                if op == "=" and actual != expected or op == "in" and actual not in expected:
                    return False
            return True

        rows = [_normalized(row) for row in self.records if row["entity"] == entity]
        rows = sorted((row for row in rows if matches(row)), key=lambda row: int(row["id"]))
        return rows[offset:offset + limit]


class OdooSource(_Source):
    """Allowlisted JSON-2 reads using the caller's existing Odoo connection."""

    def __init__(self, connection: Any, contract: TaskContract | dict):
        super().__init__(contract)
        self._connection = connection
        self._schemas: set[str] = set()

    def _raw(self, entity: str, domain: list, limit: int, offset: int) -> list[dict]:
        model = self._connection.get_model(MODELS[entity])
        fields = RAW_FIELDS[entity]
        if entity not in self._schemas:
            schema = model.fields_get(allfields=list(fields), attributes=["type", "relation"])
            _need(isinstance(schema, dict) and set(fields) <= set(schema), "source_schema_unavailable")
            self._schemas.add(entity)
        ids = model.search(domain=domain, limit=limit, offset=offset, order="id asc")
        _need(isinstance(ids, list) and len(ids) <= limit
              and all(type(identity) is int and 0 < identity <= MAX_ID for identity in ids)
              and ids == sorted(set(ids)), "invalid_source_response")
        if not ids:
            return []
        rows = model.read(ids=ids, fields=list(fields))
        if isinstance(rows, dict):
            rows = [rows]
        _need(isinstance(rows, list) and len(rows) == len(ids)
              and all(isinstance(row, dict) and set(fields) <= set(row)
                      and type(row.get("id")) is int for row in rows), "invalid_source_response")
        _need({row["id"] for row in rows} == set(ids), "source_changed_during_read")
        return sorted(rows, key=lambda row: row["id"])

    def _search(self, entity: str, domain: list, limit: int, offset: int) -> list[dict]:
        raw_domain = []
        for field, op, expected in domain:
            if field == "__query__":
                names = ["name", "ref"] if entity == "supplier" else (
                    ["name", "default_code"] if entity == "product" else ["name"])
                # Odoo wraps ilike text in %. Escape the caller's literal
                # PostgreSQL LIKE metacharacters, including SKU underscores.
                literal = expected.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
                terms = [(name, "ilike", literal) for name in names]
                if re.fullmatch(r"[1-9][0-9]{0,15}", expected) and int(expected) <= MAX_ID:
                    terms.append(("id", "=", int(expected)))
                raw_domain.extend(["|"] * (len(terms) - 1) + terms)
                continue
            if field == "id" or field.endswith("_id"):
                expected = ([False if item is None else int(item) for item in expected]
                            if op == "in" else int(expected))
            raw_domain.append((RAW_NAMES.get(field, field), op, expected))
        if entity == "supplier":
            raw_domain.append(("supplier_rank", ">", 0))
        if entity in {"purchase_order_line", "sales_order_line"}:
            raw_domain.append(("display_type", "=", False))
        return [self._normalize(entity, row) for row in self._raw(entity, raw_domain, limit, offset)]

    def _normalize(self, entity: str, row: dict) -> dict:
        if entity == "supplier":
            fields = {"name": _text(row["name"]), "ref": _text(row["ref"], optional=True)}
        elif entity == "product":
            fields = {"name": _text(row["name"]),
                      "default_code": _text(row["default_code"], optional=True),
                      "product_template_id": _id(row["product_tmpl_id"])}
        elif entity == "purchase_order":
            fields = {"name": _text(row["name"]), "state": _text(row["state"]),
                      "supplier_id": _id(row["partner_id"]),
                      "origin": _text(row["origin"], maximum=4096, optional=True)}
        elif entity == "sales_order":
            fields = {"name": _text(row["name"]), "state": _text(row["state"]),
                      "customer_id": _id(row["partner_id"]),
                      "commitment_date": _date(row["commitment_date"]),
                      "client_order_ref": _text(row["client_order_ref"], optional=True)}
        elif entity in {"purchase_order_line", "sales_order_line"}:
            purchase = entity == "purchase_order_line"
            fields = {"order_id": _id(row["order_id"]), "product_id": _id(row["product_id"]),
                      "quantity": _number(row["product_qty" if purchase else "product_uom_qty"]),
                      "unit_price": _number(row["price_unit"])}
            if purchase:
                fields.update(received_quantity=_number(row["qty_received"]),
                              planned_date=_date(row["date_planned"]))
            else:
                fields["delivered_quantity"] = _number(row["qty_delivered"])
        elif entity == "bom":
            fields = {"product_id": _id(row["product_id"], nullable=True),
                      "product_template_id": _id(row["product_tmpl_id"]),
                      "quantity": _number(row["product_qty"], positive=True),
                      "type": _text(row["type"]), "active": row["active"]}
        elif entity == "bom_line":
            fields = {"bom_id": _id(row["bom_id"]), "product_id": _id(row["product_id"]),
                      "quantity": _number(row["product_qty"], positive=True)}
        else:
            supplier_id = _id(row["partner_id"])
            supplier = self._get("supplier", supplier_id)
            _need(supplier is not None, "source_relation_missing")
            product_id = _id(row["product_id"], nullable=True)
            template_id = _id(row["product_tmpl_id"])
            products = self._search("product", [("id", "=", product_id)] if product_id else
                                    [("product_template_id", "=", template_id)], 2, 0)
            _need(bool(products), "source_relation_missing")
            product = products[0] if len(products) == 1 else None
            product_code = product["fields"]["default_code"] if product else None
            supplier_ref = supplier["fields"]["ref"]
            price = float(_number(row["price"]))
            lead, minimum = _number(row["delay"]), _number(row["min_qty"])
            _need(float(lead).is_integer() and float(minimum).is_integer(), "invalid_source_record")
            key = (f"{supplier_ref}|{product_code}|{int(minimum)}|{round(price, 2)}|{int(lead)}"
                   if supplier_ref and product_code else None)
            fields = {"supplier_id": supplier_id, "supplier_ref": supplier_ref,
                      "product_id": product["id"] if product else None,
                      "product_template_id": template_id, "product_code": product_code,
                      "unit_price": price, "lead_days": int(lead), "minimum_quantity": int(minimum),
                      "offer_key": key}
        return _normalized(_record(entity, row["id"], fields))

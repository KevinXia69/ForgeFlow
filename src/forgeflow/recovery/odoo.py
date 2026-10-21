"""Fixed Odoo 19 planning-document writes, without physical-event methods.

Internal transport for validated change sets, not a model-callable RPC escape
hatch. Callers must use the journal and live pre/postconditions. No automatic
retry is performed, including when a response is lost after a committed write.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Any


class DocumentError(RuntimeError):
    pass


def _id(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 2**53 - 1:
        raise DocumentError("invalid_record_id")
    return value


def _created_id(value: Any) -> int:
    # JSON-2 create(vals_list=[...]) returns a recordset as a singleton ID list.
    # A malformed response must never trigger an automatic create retry.
    if not isinstance(value, list) or len(value) != 1:
        raise DocumentError("invalid_create_result")
    return _id(value[0])


def _quantity(value: Any) -> int:
    if type(value) is not int or not 0 < value <= 10**9:
        raise DocumentError("invalid_quantity")
    return value


def _number(value: Any) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or value <= 0:
        raise DocumentError("invalid_positive_number")
    return float(value)


def _text(value: Any) -> str:
    if (not isinstance(value, str) or not value.strip() or len(value) > 2000
            or any(ord(char) < 32 for char in value)):
        raise DocumentError("invalid_text")
    return value


def _date(value: Any) -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
        if parsed.strftime("%Y-%m-%d %H:%M:%S") != value:
            raise ValueError
    except (ValueError, TypeError):
        raise DocumentError("invalid_naive_utc_datetime") from None
    return value


def _origin(names: list[str]) -> str:
    if not isinstance(names, list) or not names or len(names) > 500:
        raise DocumentError("invalid_consumer_names")
    for name in names:
        _text(name)
        if "," in name:
            raise DocumentError("ambiguous_consumer_name")
    if len(set(names)) != len(names):
        raise DocumentError("duplicate_consumer_names")
    return ", ".join(sorted(names))


def _relation(value: Any) -> int:
    # JSON-2 many2one reads include a display name; synthetic connections may
    # already normalize them. Neither form may introduce another target.
    if isinstance(value, list) and len(value) == 2:
        value = value[0]
    return _id(value)


class PlanDocuments:
    """Use only reviewed, fixed methods on an injected JSON-2 Odoo connection."""

    def __init__(self, connection: Any):
        self._connection = connection

    def _call(self, model: str, method: str, **kwargs):
        try:
            return getattr(self._connection.get_model(model), method)(**kwargs)
        except Exception:
            # An RPC failure may echo the request's authorization material.
            raise DocumentError("remote_operation_outcome_unknown") from None

    def create_manufacturing(
        self, *, name: str, company_id: int, product_id: int, product_uom_id: int,
        bom_id: int, quantity: int, consumer_names: list[str], start: str, deadline: str,
    ) -> int:
        values = {
            "name": _text(name), "company_id": _id(company_id),
            "product_id": _id(product_id), "product_uom_id": _id(product_uom_id),
            "bom_id": _id(bom_id), "product_qty": _quantity(quantity),
            "origin": _origin(consumer_names), "date_start": _date(start),
            "date_deadline": _date(deadline),
        }
        if start >= deadline:
            raise DocumentError("invalid_manufacturing_date_order")
        return _created_id(self._call("mrp.production", "create", vals_list=[values]))

    def confirm_manufacturing(self, record_id: int) -> None:
        self._call("mrp.production", "action_confirm", ids=[_id(record_id)])

    def cancel_manufacturing(self, record_id: int) -> None:
        self._call("mrp.production", "action_cancel", ids=[_id(record_id)])

    def set_manufacturing_quantity(self, record_id: int, quantity: int) -> None:
        # Confirmed MO header write alone leaves stock move quantities stale.
        # The native wizard updates raw/finished moves and workorder quantities.
        values = {"mo_id": _id(record_id), "product_qty": _quantity(quantity)}
        wizard = _created_id(self._call("change.production.qty", "create", vals_list=[values]))
        self._call("change.production.qty", "change_prod_qty", ids=[wizard])

    def set_manufacturing_origin(self, record_id: int, *, consumer_names: list[str]) -> None:
        # Writing an unchanged date_start can unplan the existing workorder.
        self._call("mrp.production", "write", ids=[_id(record_id)],
                   vals={"origin": _origin(consumer_names)})

    def set_manufacturing_plan(
        self, record_id: int, *, consumer_names: list[str], start: str, deadline: str,
    ) -> None:
        values = {
            "origin": _origin(consumer_names), "date_start": _date(start),
            "date_deadline": _date(deadline),
        }
        if start >= deadline:
            raise DocumentError("invalid_manufacturing_date_order")
        self._call("mrp.production", "write", ids=[_id(record_id)], vals=values)

    def set_workorder_plan(
        self, record_id: int, *, workcenter_id: int, start: str,
        finish: str, duration_minutes: float,
    ) -> None:
        values = {
            "workcenter_id": _id(workcenter_id), "date_start": _date(start),
            "date_finished": _date(finish),
            "duration_expected": _number(duration_minutes),
        }
        if start >= finish:
            raise DocumentError("invalid_workorder_date_order")
        record_id = _id(record_id)
        # A changed center recomputes duration/finish after Odoo's write, even
        # when they were supplied explicitly. Finish that native recalculation
        # first, then apply the already-validated schedule as a separate write.
        # These are two separate ERP writes, not retries or an atomic operation.
        # Callers MUST read back outcomes; an exception after either call must
        # be reconciled before repeating anything. Planned cross-calendar moves
        # additionally require the caller's unplan/calendar preconditions.
        self._call("mrp.workorder", "write", ids=[record_id],
                   vals={"workcenter_id": values.pop("workcenter_id")})
        self._call("mrp.workorder", "write", ids=[record_id], vals=values)

    def create_purchase(
        self, *, name: str, company_id: int, currency_id: int, partner_id: int,
        product_id: int, product_uom_id: int, product_name: str, quantity: int,
        price_unit: float, arrival: str, consumer_names: list[str],
    ) -> int:
        line = {
            "product_id": _id(product_id), "product_uom_id": _id(product_uom_id),
            "name": _text(product_name), "product_qty": _quantity(quantity),
            "price_unit": _number(price_unit), "date_planned": _date(arrival),
        }
        values = {
            "name": _text(name), "company_id": _id(company_id),
            "currency_id": _id(currency_id), "partner_id": _id(partner_id),
            "origin": _origin(consumer_names), "order_line": [(0, 0, line)],
        }
        return _created_id(self._call("purchase.order", "create", vals_list=[values]))

    def confirm_purchase(self, record_id: int) -> None:
        self._call("purchase.order", "button_confirm", ids=[_id(record_id)])

    def cancel_purchase(self, record_id: int) -> None:
        self._call("purchase.order", "button_cancel", ids=[_id(record_id)])

    def set_purchase_origin(self, record_id: int, *, consumer_names: list[str]) -> None:
        self._call("purchase.order", "write", ids=[_id(record_id)],
                   vals={"origin": _origin(consumer_names)})

    def set_purchase_line(
        self, line_id: int, *, quantity: int, price_unit: float, arrival: str,
    ) -> None:
        values = {"product_qty": _quantity(quantity), "price_unit": _number(price_unit),
                  "date_planned": _date(arrival)}
        line_id = _id(line_id)
        line, move, picking = self._purchase_receipt(line_id)
        arrival_changed = line["date_planned"] != arrival
        self._call("purchase.order.line", "write", ids=[line_id], vals=values)
        if arrival_changed and move is not None:
            # Pinned Odoo updates the line and move deadline, but leaves the
            # move date / receipt schedule stale. Only the already related,
            # unexecuted singleton receipt may follow the approved arrival.
            # These calls are not atomic and must never be retried internally.
            # The readback adapter and unchanged whole-scope guard verify them.
            self._call("stock.picking", "write", ids=[picking["id"]],
                       vals={"scheduled_date": arrival})
            self._call("stock.move", "write", ids=[move["id"]],
                       vals={"date": arrival, "date_deadline": arrival})

    def _read_record(self, model: str, record_id: int, fields: list[str]) -> dict:
        rows = self._call(model, "read", ids=[record_id], fields=fields)
        # Pinned JSON-2 JsonModel.read unwraps one record into a dictionary.
        # Normalize only that shape; preserve all identity/completeness checks.
        if isinstance(rows, dict):
            rows = [rows]
        if (not isinstance(rows, list) or len(rows) != 1
                or not isinstance(rows[0], dict) or not set(fields) <= rows[0].keys()
                or type(rows[0]["id"]) is not int or rows[0]["id"] != record_id):
            raise DocumentError("incomplete_purchase_receipt_read")
        return rows[0]

    def _purchase_receipt(self, line_id: int) -> tuple[dict, dict | None, dict | None]:
        """Resolve only a line's original singleton, unexecuted receipt chain.

        The caller's scope guard binds these relationships to the approved
        baseline. Recheck them here before any write, including both backlinks,
        so a shared receipt cannot reschedule other purchase lines.
        """
        line = self._read_record("purchase.order.line", _id(line_id), [
            "id", "order_id", "product_id", "product_uom_id", "product_qty",
            "qty_received", "date_planned", "display_type", "move_ids",
        ])
        po = self._read_record("purchase.order", _relation(line["order_id"]), [
            "id", "state", "company_id", "order_line", "picking_ids",
        ])
        if (po["order_line"] != [line_id] or line["display_type"] is not False
                or type(line["qty_received"]) not in (int, float)
                or line["qty_received"] != 0):
            raise DocumentError("purchase_receipt_binding_conflict")
        if po["state"] == "draft":
            if line["move_ids"] != [] or po["picking_ids"] != []:
                raise DocumentError("draft_purchase_has_receipt")
            return line, None, None
        if (po["state"] != "purchase" or not isinstance(line["move_ids"], list)
                or len(line["move_ids"]) != 1 or not isinstance(po["picking_ids"], list)
                or len(po["picking_ids"]) != 1):
            raise DocumentError("unsupported_purchase_receipt")
        move = self._read_record("stock.move", _id(line["move_ids"][0]), [
            "id", "purchase_line_id", "picking_id", "product_id", "product_uom",
            "product_uom_qty", "company_id", "raw_material_production_id", "production_id",
            "state", "picked", "date", "date_deadline",
        ])
        picking_id = _id(po["picking_ids"][0])
        if (_relation(move["purchase_line_id"]) != line_id
                or _relation(move["picking_id"]) != picking_id
                or _relation(move["product_id"]) != _relation(line["product_id"])
                or _relation(move["product_uom"]) != _relation(line["product_uom_id"])
                or _relation(move["company_id"]) != _relation(po["company_id"])
                or move["product_uom_qty"] != line["product_qty"]
                or move["raw_material_production_id"] or move["production_id"]
                or move["picked"] is not False
                or move["state"] not in ("confirmed", "waiting", "assigned", "partially_available")):
            raise DocumentError("purchase_receipt_binding_conflict")
        picking = self._read_record("stock.picking", picking_id, [
            "id", "move_ids", "state", "date_done", "scheduled_date",
        ])
        if (picking["move_ids"] != [move["id"]] or picking["date_done"]
                or picking["state"] not in ("confirmed", "waiting", "assigned")):
            raise DocumentError("purchase_receipt_binding_conflict")
        return line, move, picking

    def purchase_arrival_matches(self, line_id: int, arrival: str) -> bool:
        """Read back every receipt date; a line-only write is not completion."""
        arrival = _date(arrival)
        line, move, picking = self._purchase_receipt(line_id)
        return line["date_planned"] == arrival and (
            move is None or (move["date"] == arrival and move["date_deadline"] == arrival
                             and picking["scheduled_date"] == arrival)
        )

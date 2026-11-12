"""Read the facts a replacement plan is allowed to rely on from Odoo 20.

The collector is deliberately read-only and small: a cancelled purchase order,
its lines, the supplier offers that exist for the product, stock on hand and the
open commitments that can still cover part of the loss.  Anything it cannot read
is reported as missing rather than assumed -- the planner then reports a
shortfall instead of inventing supply.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from ...planning.replacement import (
    OpenCommitment,
    Period,
    PurchaseOffer,
    ReplacementDemand,
    ReplacementPlanner,
    parse_odoo_datetime,
    periods_until,
)
from .supplier_prices import applicable_supplier_prices

#: Purchase-line states that still represent a commitment.
OPEN_LINE_STATES = ("draft", "sent", "purchase", "to approve")


class ProcurementError(ValueError):
    """A bounded, machine-readable refusal."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class ReplacementCase:
    """The read-only Odoo facts a replacement decision may use."""

    order: dict[str, Any]
    line: dict[str, Any]
    product: dict[str, Any]
    company_id: int
    currency: str
    unit: str
    required_quantity: int
    due_date: date
    stock: int
    commitments: tuple[OpenCommitment, ...]
    offers: tuple[PurchaseOffer, ...]
    today: date
    periods: tuple[Period, ...]
    due_period: int

    def planner(self, *, time_limit_seconds: float = 10.0) -> ReplacementPlanner:
        return ReplacementPlanner(
            periods=list(self.periods),
            demand=ReplacementDemand(
                product_id=int(self.product["id"]),
                product_code=str(self.product.get("default_code")
                                 or self.product.get("name") or self.product["id"]),
                quantity=int(self.required_quantity),
                due_period=int(self.due_period),
                origin_refs=({"model": "purchase.order",
                              "id": int(self.order["id"])},),
            ),
            stock=int(self.stock),
            commitments=list(self.commitments),
            offers=list(self.offers),
            time_limit_seconds=time_limit_seconds,
        )


def _single_id(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        return int(value[0]) if value else 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_date(value: Any) -> date | None:
    parsed = parse_odoo_datetime(value)
    return parsed.date() if parsed else None


def collect_replacement_case(adapter, *, order_id: int,
                             today: date | None = None,
                             product_id: int | None = None) -> ReplacementCase:
    """Read one cancelled purchase order and everything needed to replace it."""
    moment = today or datetime.now(UTC).date()
    orders = adapter.search_read(
        "purchase.order", [("id", "=", int(order_id))],
        ["id", "name", "state", "partner_id", "company_id", "date_planned",
         "date_order", "currency_id"], limit=1)
    if not orders:
        raise ProcurementError("purchase_order_not_found")
    order = orders[0]
    lines = adapter.search_read(
        "purchase.order.line", [("order_id", "=", int(order_id))],
        ["id", "product_id", "product_qty", "qty_received", "price_unit",
         "date_planned", "company_id"], limit=200)
    if not lines:
        raise ProcurementError("purchase_order_has_no_lines")
    if product_id is not None:
        lines = [line for line in lines
                 if _single_id(line.get("product_id")) == int(product_id)]
        if not lines:
            raise ProcurementError("purchase_order_has_no_lines")
    products = {_single_id(line.get("product_id")) for line in lines}
    products.discard(0)
    if len(products) != 1:
        # One committed product per replacement decision; mixing products would
        # make the lost quantity and the supplier offers ambiguous.
        raise ProcurementError("multi_product_commitment_unsupported")
    line = lines[0]
    product_id = _single_id(line["product_id"])
    rows = adapter.search_read(
        "product.product", [("id", "=", product_id)],
        ["id", "name", "default_code", "product_tmpl_id", "type", "is_storable",
         "uom_id"], limit=1)
    if not rows:
        raise ProcurementError("product_not_found")
    product = rows[0]
    company_id = _single_id(order.get("company_id"))
    currency = _currency_name(adapter, order, company_id)

    received = sum(float(item.get("qty_received") or 0) for item in lines)
    required = int(round(sum(float(item.get("product_qty") or 0) for item in lines)))
    if str(order.get("state")) == "done" or (required <= 0):
        raise ProcurementError("commitment_not_replaceable")
    if received > 0 and str(order.get("state")) != "cancel":
        raise ProcurementError("commitment_partially_received")
    due = _as_date(line.get("date_planned")) or _as_date(
        order.get("date_planned")) or moment
    due = max(due, moment)
    periods = periods_until(moment, due)
    due_period = max(0, (due - moment).days)

    stock = _stock_on_hand(adapter, product, company_id)
    commitments = _open_commitments(adapter, product_id, company_id, order_id,
                                    moment, due_period)
    offers = _offers(adapter, product, company_id, currency, as_of=moment)
    unit = str(((product.get("uom_id") or [None, "Units"])[1]
                if isinstance(product.get("uom_id"), (list, tuple))
                else "Units") or "Units")
    return ReplacementCase(
        order=order, line=line, product=product, company_id=company_id,
        currency=currency, unit=unit, required_quantity=max(0, required - int(received)),
        due_date=due, stock=stock, commitments=commitments, offers=offers,
        today=moment, periods=tuple(periods), due_period=due_period)


def _currency_name(adapter, order: dict[str, Any], company_id: int) -> str:
    currency_id = _single_id(order.get("currency_id"))
    if currency_id:
        rows = adapter.search_read("res.currency", [("id", "=", currency_id)],
                                   ["id", "name"], limit=1)
        if rows:
            return str(rows[0]["name"])
    if company_id:
        rows = adapter.search_read("res.company", [("id", "=", company_id)],
                                   ["id", "currency_id"], limit=1)
        if rows:
            nested = _single_id(rows[0].get("currency_id"))
            if nested:
                currencies = adapter.search_read(
                    "res.currency", [("id", "=", nested)], ["id", "name"], limit=1)
                if currencies:
                    return str(currencies[0]["name"])
    return "USD"


def _stock_on_hand(adapter, product: dict[str, Any], company_id: int) -> int:
    if not product.get("is_storable"):
        # A consumable is not stock-managed; reporting a stock figure for it
        # would be an invented fact.
        return 0
    quants = adapter.search_read(
        "stock.quant", [("product_id", "=", int(product["id"]))],
        ["quantity", "reserved_quantity", "location_id", "company_id"], limit=500)
    location_ids = {_single_id(row.get("location_id")) for row in quants}
    location_ids.discard(0)
    usages = {}
    if location_ids:
        for row in adapter.search_read(
                "stock.location", [("id", "in", sorted(location_ids))],
                ["id", "usage"], limit=500):
            usages[int(row["id"])] = str(row.get("usage") or "")
    total = 0
    for row in quants:
        if company_id and row.get("company_id") and (
                _single_id(row.get("company_id")) != company_id):
            continue
        if usages and usages.get(_single_id(row.get("location_id"))) != "internal":
            continue
        available = float(row.get("quantity") or 0) - float(
            row.get("reserved_quantity") or 0)
        total += max(0, int(available))
    return total


def collect_replacement_cases(adapter, *, order_id: int,
                              today: date | None = None) -> tuple[ReplacementCase, ...]:
    """Collect one replacement case per product on a multi-product commitment."""
    lines = adapter.search_read(
        "purchase.order.line", [("order_id", "=", int(order_id))],
        ["id", "product_id"], limit=200)
    if not lines:
        raise ProcurementError("purchase_order_has_no_lines")
    product_ids = []
    for line in lines:
        pid = _single_id(line.get("product_id"))
        if pid and pid not in product_ids:
            product_ids.append(pid)
    if not product_ids:
        raise ProcurementError("purchase_order_has_no_lines")
    return tuple(
        collect_replacement_case(adapter, order_id=order_id, today=today,
                                 product_id=pid)
        for pid in product_ids)


def _open_commitments(adapter, product_id: int, company_id: int, order_id: int,
                      today: date, due_period: int) -> tuple[OpenCommitment, ...]:
    domain: list[Any] = [("product_id", "=", product_id),
                         ("state", "in", list(OPEN_LINE_STATES))]
    if company_id:
        domain.append(("company_id", "=", company_id))
    rows = adapter.search_read(
        "purchase.order.line", domain,
        ["id", "order_id", "product_qty", "qty_received", "price_unit",
         "date_planned"], limit=200)
    result = []
    for row in rows:
        if _single_id(row.get("order_id")) == int(order_id):
            continue
        arrival = _as_date(row.get("date_planned"))
        if arrival is None:
            continue
        available_period = max(0, (arrival - today).days)
        if available_period > due_period:
            continue
        # Received units are already in stock; counting the full ordered
        # quantity would double-count them against the same demand.
        quantity = int(round(float(row.get("product_qty") or 0)
                             - float(row.get("qty_received") or 0)))
        if quantity <= 0:
            continue
        result.append(OpenCommitment(
            line_id=int(row["id"]), partner_id=0, quantity=quantity,
            available_period=available_period,
            unit_price=float(row.get("price_unit") or 0)))
    return tuple(result)


def _offers(adapter, product: dict[str, Any], company_id: int,
            currency: str, *, as_of: date | None = None) -> tuple[PurchaseOffer, ...]:
    rows = applicable_supplier_prices(
        adapter, product, company_id, as_of=as_of or datetime.now(UTC).date())
    offers = []
    for row in rows:
        partner = _single_id(row.get("partner_id"))
        if partner <= 0:
            continue
        row_company = _single_id(row.get("company_id"))
        if row_company and company_id and row_company != company_id:
            continue
        offer_currency = _currency_name(
            adapter, {"currency_id": row.get("currency_id")}, company_id)
        if currency and offer_currency and offer_currency != currency:
            # The planner compares offer prices directly; silently treating a
            # foreign-currency quote as the order currency would be wrong.
            raise ProcurementError("supplier_offer_currency_mismatch")
        price = float(row.get("price") or 0)
        if price <= 0:
            # A zero or missing price is not an offer: publishing it would make a
            # free purchase look cheapest.
            continue
        delay = row.get("delay")
        lead = int(delay) if isinstance(delay, (int, float)) and delay >= 0 else 0
        offers.append(PurchaseOffer(
            supplier_id=partner, supplier_ref=f"partner:{partner}", price=price,
            currency=offer_currency or currency, min_order_quantity=max(0, int(row.get("min_qty") or 0)),
            capacity_per_period=None, lead_periods=lead))
    return tuple(offers)

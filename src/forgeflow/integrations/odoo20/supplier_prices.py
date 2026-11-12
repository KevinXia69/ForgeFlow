"""Read vendor prices applicable to one variant at the inquiry date."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any


def _id(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        value = value[0] if value else 0
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _within_dates(row: dict, as_of: date) -> bool:
    for field, is_start in (("date_start", True), ("date_end", False)):
        raw = row.get(field)
        if raw is None or raw is False or raw == "":
            continue
        try:
            boundary = (raw.date() if isinstance(raw, datetime)
                        else raw if isinstance(raw, date)
                        else date.fromisoformat(str(raw)))
        except (TypeError, ValueError):
            # An unreadable boundary is not an unlimited price agreement.
            return False
        if (is_start and as_of < boundary) or (not is_start and as_of > boundary):
            return False
    return True


def applicable_supplier_prices(adapter, product: dict, company_id: int, *,
                               as_of: date, limit: int = 200) -> list[dict]:
    """Keep generic or exact-variant prices whose inclusive dates cover as_of.

    Recovery creates its RFQs now; planned receipt dates are not price inquiry
    dates. Both recovery paths pass their captured planning date explicitly.
    Query filters precede the row limit, and response checks also protect callers
    using an adapter that returns a broader set of observations.
    """
    template = _id(product.get("product_tmpl_id"))
    product_id = _id(product.get("id"))
    if not template:
        return []
    domain: list = [("product_tmpl_id", "=", template)]
    if product_id:
        domain.extend(["|", ("product_id", "=", False), ("product_id", "=", product_id)])
    else:
        domain.append(("product_id", "=", False))
    domain.extend([
        "|", ("date_start", "=", False), ("date_start", "<=", as_of.isoformat()),
        "|", ("date_end", "=", False), ("date_end", ">=", as_of.isoformat()),
    ])
    if company_id:
        domain.extend(["|", ("company_id", "=", False), ("company_id", "=", company_id)])
    rows = adapter.search_read(
        "product.supplierinfo", domain,
        ["id", "partner_id", "product_tmpl_id", "product_id", "min_qty", "price",
         "delay", "company_id", "currency_id", "date_start", "date_end"], limit=limit)
    return [row for row in rows
            if (not _id(row.get("product_tmpl_id"))
                or _id(row.get("product_tmpl_id")) == template)
            and (not _id(row.get("product_id")) or _id(row.get("product_id")) == product_id)
            and (not company_id or not _id(row.get("company_id"))
                 or _id(row.get("company_id")) == company_id)
            and _within_dates(row, as_of)]

"""Translate a read-only Odoo BOM tree into the W7 planner contract."""
from __future__ import annotations

from typing import Any

from .multi_bom import BomError, BomNode, Product


def _number(value: Any, code: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise BomError(code) from None
    if number <= 0:
        raise BomError(code)
    return number


def bom_node_from_odoo(tree: dict[str, Any]) -> BomNode:
    children = []
    for line in tree.get("lines", []):
        per_parent = _number(line.get("quantity"), "bom_invalid_quantity")
        number = int(per_parent)
        if per_parent != number:
            raise BomError("bom_fractional_quantity_unsupported")
        child = line.get("bom")
        if child:
            base = bom_node_from_odoo(child)
            children.append(BomNode(base.code, number, base.children))
        else:
            children.append(BomNode(str(line["product_id"]), number))
    return BomNode(str(tree["product_id"]), 1, tuple(children))


def product_from_odoo(tree: dict[str, Any], *, code: str, demand: int,
                      resource_hours: dict[str, float] | None = None) -> Product:
    return Product(code, demand, bom_node_from_odoo(tree), resource_hours or {})

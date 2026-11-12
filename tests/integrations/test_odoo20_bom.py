from __future__ import annotations

import pytest

from forgeflow.integrations.odoo20 import Odoo20Adapter, Odoo20Config, Odoo20Error


class FakeBomAdapter(Odoo20Adapter):
    def __init__(self, *, cycle: bool = False):
        super().__init__(Odoo20Config("http://fake", "db", "user", "password"))
        self.cycle = cycle

    def search_read(self, model, domain, fields, limit=100, order=None):
        if model == "product.product":
            product_id = domain[0][2]
            return [{"id": product_id, "default_code": f"P{product_id}",
                     "product_tmpl_id": [product_id, f"P{product_id}"],
                     "uom_id": [1, "Units"]}]
        if model == "mrp.bom":
            template = domain[0][2]
            if template == 10:
                return [{"id": 100, "product_qty": 1.0, "type": "normal"}]
            if template == 20:
                return [{"id": 200, "product_qty": 1.0, "type": "normal"}]
            return []
        if model == "mrp.bom.line":
            bom = domain[0][2]
            if bom == 100:
                return [{"product_id": [20, "P20"], "product_qty": 2.0}]
            if bom == 200 and self.cycle:
                return [{"product_id": [10, "P10"], "product_qty": 1.0}]
            return []
        raise AssertionError(model)


def test_reads_nested_bom_lines() -> None:
    tree = FakeBomAdapter().read_bom_tree(10)
    assert tree is not None and tree["bom_id"] == 100
    assert tree["lines"][0]["product_id"] == 20
    assert tree["lines"][0]["quantity"] == 2.0
    assert tree["lines"][0]["bom"]["bom_id"] == 200


def test_rejects_bom_cycle() -> None:
    with pytest.raises(Odoo20Error, match="bom_cycle_or_depth"):
        FakeBomAdapter(cycle=True).read_bom_tree(10)

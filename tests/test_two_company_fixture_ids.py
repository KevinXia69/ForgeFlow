"""The fixture must put product.product variant ids into BOM and order lines."""
from __future__ import annotations

import importlib.util
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[1] / "scripts/accept_odoo20_two_companies.py"
_spec = importlib.util.spec_from_file_location("fixture_two_companies", SCRIPT)
assert _spec and _spec.loader
fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fixture)


class _FakeOdoo:
    def __init__(self) -> None:
        self.created: list[tuple[str, dict]] = []
        self.writes: list[tuple[str, str, list]] = []
        self.lines: list[dict] = []

    def ensure(self, model, domain, values, *, company=None):
        return {"product.template": 100, "mrp.bom": 900}.get(model, 1)

    def search(self, model, domain, fields, *, company=None):
        if model == "product.product":
            return [{"id": 500}]
        if model.endswith(".line"):
            return self.lines
        return []

    def create(self, model, values, *, company=None):
        self.created.append((model, dict(values)))
        return 901

    def call(self, model, method, args, *, company=None, **kwargs):
        self.writes.append((model, method, list(args)))
        return True


def test_product_returns_both_the_template_and_a_distinct_variant() -> None:
    odoo = _FakeOdoo()
    template, variant = fixture._product(odoo, 8, "X", "X product")
    assert template == 100 and variant == 500


def test_bom_lines_receive_the_variant_not_the_template() -> None:
    odoo = _FakeOdoo()
    bom = fixture._bom(odoo, 8, 100, [(500, 2.0)])
    assert bom == 900
    assert odoo.created == [("mrp.bom.line", {
        "bom_id": 900, "product_id": 500, "product_qty": 2.0})]


def test_an_existing_bom_line_is_repaired_to_the_variant() -> None:
    odoo = _FakeOdoo()
    odoo.lines = [{"id": 77, "product_id": [100], "product_qty": 2.0}]
    fixture._bom(odoo, 8, 100, [(500, 2.0)])
    assert odoo.writes == [("mrp.bom.line", "write",
                            [[77], {"product_id": 500, "product_qty": 2.0}])]


def test_existing_order_lines_are_repaired_to_the_variant() -> None:
    odoo = _FakeOdoo()
    odoo.lines = [{"id": 88, "product_id": [100]}]
    fixture._repair_line(odoo, "sale.order.line", 67, 500, 8)
    assert odoo.writes == [("sale.order.line", "write",
                            [[88], {"product_id": 500}])]

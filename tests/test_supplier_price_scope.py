"""Recovery uses prices for the exact variant and the current inquiry date."""
from __future__ import annotations

from datetime import date

import pytest

from forgeflow.integrations.odoo20.adapter import Odoo20Adapter, Odoo20Config
from forgeflow.integrations.odoo20.procurement import (
    ProcurementError,
    _offers,
    collect_replacement_case,
)
from forgeflow.superagent.manufacturing import _offer

AS_OF = date(2026, 11, 10)
PRODUCT = {"id": 42, "product_tmpl_id": [7, "Relay"],
           "name": "24 V relay", "uom_id": [1, "Units"], "is_storable": False}


def price(**values):
    return {"id": 1, "partner_id": [10, "Supplier"],
            "product_tmpl_id": [7, "Relay"], "product_id": False,
            "price": 8.0, "min_qty": 0, "delay": 0,
            "currency_id": [1, "USD"], "company_id": [1, "Company"],
            "date_start": False, "date_end": False, **values}


class Prices:
    """Project requested fields, like XML-RPC, so omitted fields cannot pass."""

    def __init__(self, rows):
        self.rows = rows
        self.requests = []

    def search_read(self, model, domain, fields, limit=100, **kwargs):
        self.requests.append((model, domain, fields))
        if model == "product.supplierinfo":
            return [{key: row[key] for key in fields if key in row} for row in self.rows]
        if model == "res.currency":
            return [{"id": domain[0][2], "name": "EUR" if domain[0][2] == 2 else "USD"}]
        if model == "purchase.order":
            return [{"id": 5, "name": "P5", "state": "cancel", "company_id": [1, "Co"],
                     "currency_id": [1, "USD"], "date_planned": "2024-01-15 09:00:00"}]
        if model == "purchase.order.line":
            if domain == [("order_id", "=", 5)]:
                return [{"id": 8, "product_id": [42, "Relay"], "product_qty": 3,
                         "qty_received": 0, "date_planned": "2024-01-15 09:00:00"}]
            return []
        if model == "product.product":
            return [PRODUCT]
        return []


def selected(kind, rows, **kwargs):
    adapter = Prices(rows)
    if kind == "purchase":
        return [row.supplier_id for row in _offers(adapter, PRODUCT, 1, "USD", **kwargs)]
    result = _offer(adapter, PRODUCT, 1, "USD", **kwargs)
    return [result["partner_id"]] if result else []


@pytest.mark.parametrize("kind", ["purchase", "manufacturing"])
@pytest.mark.parametrize("fields", [
    {"product_id": [99, "12 V relay"]},
    {"date_end": "2026-11-09"},
    {"date_start": "2026-11-11"},
    {"date_start": "bad-date"},
    {"date_end": "bad-date"},
    {"company_id": [2, "Other company"]},
])
def test_inapplicable_cheap_price_cannot_win(kind, fields):
    assert selected(kind, [price(price=1.0, **fields),
                           price(id=2, partner_id=[20, "Current supplier"],
                                 product_id=[42, "24 V relay"], price=9.0)],
                    as_of=AS_OF) == [20]


@pytest.mark.parametrize("kind", ["purchase", "manufacturing"])
@pytest.mark.parametrize("fields", [
    {},
    {"product_id": [42, "24 V relay"]},
    {"date_start": "2026-11-10", "date_end": "2026-11-10"},
    {"company_id": False, "date_start": "2026-01-01"},
])
def test_generic_and_exact_variant_prices_include_both_validity_endpoints(kind, fields):
    assert selected(kind, [price(**fields)], as_of=AS_OF) == [10]


def test_replacement_collection_uses_its_snapshot_date_not_the_wall_clock():
    adapter = Prices([
        price(id=2, partner_id=[11, "Wrong variant"], product_id=[99, "12 V"], price=1.0),
        price(id=3, partner_id=[12, "Expired price"], date_end="2024-01-09", price=2.0),
        price(date_start="2024-01-01", date_end="2024-01-31"),
    ])
    case = collect_replacement_case(adapter, order_id=5, today=date(2024, 1, 10))
    assert len(case.offers) == 1
    plan = case.planner().solve()
    assert plan.executable
    assert plan.buy_by_supplier() == {"partner:10": 3}
    assert plan.cost == pytest.approx(24.0)


def test_no_applicable_purchase_prices_produces_a_shortfall():
    adapter = Prices([price(date_end="2024-01-09")])
    case = collect_replacement_case(adapter, order_id=5, today=date(2024, 1, 10))
    plan = case.planner().solve()
    assert plan.unmet == 3
    assert not plan.executable


def test_no_applicable_component_prices_returns_no_manufacturing_offer():
    assert _offer(Prices([price(date_end="2026-11-09")]), PRODUCT, 1, "USD",
                  as_of=AS_OF) is None


def test_real_adapter_keeps_offer_identity_validity_and_currency_fields():
    observed = []

    class Models:
        def execute_kw(self, database, uid, password, model, method, args, kwargs):
            observed.append((model, args, kwargs))
            if model == "product.supplierinfo":
                row = price(currency_id=[2, "EUR"])
                return [{key: row[key] for key in kwargs["fields"] if key in row}]
            if model == "res.currency":
                return [{"id": 2, "name": "EUR"}]
            return []

    adapter = Odoo20Adapter(Odoo20Config("http://odoo", "db", "user", "test"))
    adapter._models = lambda: Models()
    adapter._authenticate = lambda: 1
    with pytest.raises(ProcurementError, match="supplier_offer_currency_mismatch"):
        _offers(adapter, PRODUCT, 1, "USD", as_of=AS_OF)
    model, args, kwargs = observed[0]
    assert model == "product.supplierinfo"
    assert {"product_id", "date_start", "date_end", "currency_id"} <= set(kwargs["fields"])
    assert ("product_id", "=", 42) in args[0]
    assert ("date_start", "<=", "2026-11-10") in args[0]
    assert ("date_end", ">=", "2026-11-10") in args[0]


@pytest.mark.parametrize("kind", ["purchase", "manufacturing"])
def test_an_expired_foreign_currency_price_does_not_block_a_current_price(kind):
    assert selected(kind, [price(currency_id=[2, "EUR"], date_end="2026-11-09"),
                           price(id=2, partner_id=[20, "Current supplier"])],
                    as_of=AS_OF) == [20]

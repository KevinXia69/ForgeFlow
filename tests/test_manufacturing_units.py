"""Manufacturing recovery helpers: stock scope, BOM base, SKU-less components."""
from __future__ import annotations

from datetime import date

import pytest

from forgeflow.superagent.manufacturing import (
    ManufacturingPlanError,
    _bom_tree,
    _component_product,
    _remaining_quantity,
    _stock,
    _workcenter_data,
)


class Adapter:
    def __init__(self, rows, locations=None, bom=None, lines=None):
        self.rows = rows
        self.locations = locations or []
        self.bom = bom
        self.lines = lines or []

    def search_read(self, model, domain, fields, limit=100, order=None):
        if model == "stock.quant":
            return self.rows
        if model == "stock.location":
            return self.locations
        if model == "mrp.bom":
            return [self.bom] if self.bom else []
        if model == "mrp.bom.line":
            return self.lines
        if model == "product.product":
            if domain and domain[0][0] == "default_code":
                return []
            return [{"id": 9, "default_code": False, "name": "No SKU",
                     "product_tmpl_id": [1, "No SKU"], "uom_id": [1, "Units"]}]
        return []


def test_stock_excludes_external_and_other_company_locations() -> None:
    adapter = Adapter(
        rows=[
            {"quantity": 5, "reserved_quantity": 1, "location_id": [1, "Stock"],
             "company_id": [1, "Co"]},
            {"quantity": 100, "reserved_quantity": 0, "location_id": [2, "Customers"],
             "company_id": [1, "Co"]},
            {"quantity": 7, "reserved_quantity": 0, "location_id": [3, "Other"],
             "company_id": [2, "Other Co"]},
        ],
        locations=[{"id": 1, "usage": "internal"}, {"id": 2, "usage": "customer"},
                   {"id": 3, "usage": "internal"}])
    assert _stock(adapter, 1, 1) == 4


def test_bom_output_base_is_normalised_and_fractional_is_rejected() -> None:
    product = {"id": 1, "default_code": "FG", "product_tmpl_id": [1, "FG"],
               "uom_id": [1, "Units"]}
    adapter = Adapter(
        rows=[], bom={"id": 5, "product_qty": 10, "type": "normal"},
        lines=[{"id": 1, "product_id": [2, "C"], "product_qty": 20, "uom_id": [1, "Units"]}])
    tree = _bom_tree(adapter, product, bom_id=5)
    assert tree.children[0].per_parent == 2
    adapter.lines = [{"id": 1, "product_id": [2, "C"], "product_qty": 5,
                      "uom_id": [1, "Units"]}]
    with pytest.raises(ManufacturingPlanError, match="manufacturing_bom_fractional"):
        _bom_tree(adapter, product, bom_id=5)


def test_a_component_without_a_sku_is_found_by_its_id() -> None:
    adapter = Adapter(rows=[])
    product = _component_product(adapter, "9")
    assert product is not None and product["id"] == 9


def test_a_workcenter_without_a_calendar_blocks_the_plan() -> None:
    class NoCalendar(Adapter):
        def search_read(self, model, domain, fields, limit=100, order=None):
            if model == "mrp.routing.workcenter":
                return [{"workcenter_id": [4, "WC"], "time_cycle": 30,
                         "time_cycle_manual": 30}]
            if model == "mrp.workcenter":
                return [{"id": 4, "name": "WC", "resource_calendar_id": False}]
            return []

    with pytest.raises(ManufacturingPlanError, match="workcenter_calendar_unknown"):
        _workcenter_data(NoCalendar(rows=[]), 1, start=date(2026, 11, 9),
                         end=date(2026, 11, 11), exclude_mo_ids=set())


def test_partial_production_uses_remaining_quantity_only() -> None:
    assert _remaining_quantity({"product_qty": 10, "qty_produced": 8}) == 2
    assert _remaining_quantity({"product_qty": 10, "qty_produced": 10}) == 0


class CurrencyAdapter:
    def search_read(self, model, domain, fields, limit=100, order=None):
        if model == "product.supplierinfo":
            return [{"partner_id": [5, "Vendor"], "price": 9.0, "min_qty": 0,
                     "delay": 0, "company_id": [1, "Co"], "currency_id": [2, "EUR"]}]
        if model == "res.currency":
            return [{"id": 2, "name": "EUR"}]
        if model == "res.company":
            return [{"id": 1, "currency_id": [2, "EUR"]}]
        return []


def test_manufacturing_offer_uses_the_business_currency() -> None:
    from forgeflow.superagent.manufacturing import _offer

    product = {"product_tmpl_id": [1, "T"]}
    assert _offer(CurrencyAdapter(), product, 1, "EUR") is not None
    assert _offer(CurrencyAdapter(), product, 1, "USD") is None


class CapacityAdapter:
    def __init__(self, workorders):
        self.workorders = workorders

    def search_read(self, model, domain, fields, limit=100, order=None):
        if model == "mrp.routing.workcenter":
            return [{"workcenter_id": [4, "WC"], "time_cycle": 60,
                     "time_cycle_manual": 60}]
        if model == "mrp.workcenter":
            return [{"id": 4, "name": "WC", "resource_calendar_id": [1, "Cal"]}]
        if model == "resource.calendar.attendance":
            return [{"dayofweek": str(day), "duration_hours": 8.0}
                    for day in range(7)]
        if model == "resource.calendar.leaves":
            return []
        if model == "mrp.workorder":
            return self.workorders
        return []


def test_workorder_load_outside_the_window_is_not_subtracted() -> None:
    outside = {"production_id": [1, "MO"], "duration_expected": 600,
               "state": "confirmed", "date_start": "2026-11-20 08:00:00",
               "date_finished": "2026-11-20 18:00:00"}
    _, capacities = _workcenter_data(
        CapacityAdapter([outside]), 1, start=date(2026, 11, 9),
        end=date(2026, 11, 10), exclude_mo_ids=set())
    assert capacities[0].hours == 16.0
    inside = {**outside, "date_start": "2026-11-09 08:00:00",
              "date_finished": "2026-11-09 18:00:00"}
    _, capacities = _workcenter_data(
        CapacityAdapter([inside]), 1, start=date(2026, 11, 9),
        end=date(2026, 11, 10), exclude_mo_ids=set())
    assert capacities[0].hours == 6.0


def _entries(deadlines, hours=6.0):
    entries = {}
    for index, due in enumerate(deadlines):
        code = f"P{index}"
        entries[code] = {
            "bom_id": 1, "hours": {"WC": 1.0},
            "details": [{"mo": {"id": index + 1, "name": code},
                         "demand": int(hours), "start": date(2026, 11, 9),
                         "due": due}],
        }
    return entries


def test_cumulative_same_day_load_is_rejected(monkeypatch) -> None:
    from forgeflow.planning.multi_bom import Capacity
    import forgeflow.superagent.manufacturing as manufacturing

    monkeypatch.setattr(
        manufacturing, "_workcenter_data",
        lambda *args, **kwargs: ({}, [Capacity("WC", 8.0)]))
    with pytest.raises(ManufacturingPlanError, match="manufacturing_time_infeasible"):
        manufacturing._check_cumulative_capacity(
            object(), _entries([date(2026, 11, 9), date(2026, 11, 9)]),
            exclude_mo_ids=set())


def test_cumulative_later_deadline_is_not_over_rejected(monkeypatch) -> None:
    from forgeflow.planning.multi_bom import Capacity
    import forgeflow.superagent.manufacturing as manufacturing

    monkeypatch.setattr(
        manufacturing, "_workcenter_data",
        lambda *args, **kwargs: ({}, [Capacity("WC", 8.0)]))
    manufacturing._check_cumulative_capacity(
        object(), _entries([date(2026, 11, 9), date(2026, 11, 10)]),
        exclude_mo_ids=set())


def test_manufacturing_errors_map_to_fact_failure_or_system() -> None:
    assert ManufacturingPlanError("workcenter_calendar_unknown", "x").need == "fact"
    assert ManufacturingPlanError("bom_not_found", "x").need == "fact"
    assert ManufacturingPlanError("manufacturing_time_infeasible", "x").need == "failure"
    assert ManufacturingPlanError("manufacturing_component_late", "x").need == "failure"
    assert ManufacturingPlanError("manufacturing_incomplete", "x").need == "system"


def test_cumulative_capacity_respects_earliest_start(monkeypatch) -> None:
    from forgeflow.planning.multi_bom import Capacity
    import forgeflow.superagent.manufacturing as manufacturing

    monkeypatch.setattr(
        manufacturing, "_workcenter_data",
        lambda *args, **kwargs: ({}, [Capacity("WC", 8.0)]))
    entries = {
        "A": {"bom_id": 1, "hours": {"WC": 1.0},
              "details": [{"mo": {"id": 1}, "demand": 2,
                           "start": date(2026, 11, 9), "due": date(2026, 11, 9)}]},
        "B": {"bom_id": 1, "hours": {"WC": 1.0},
              "details": [{"mo": {"id": 2}, "demand": 6,
                           "start": date(2026, 11, 11), "due": date(2026, 11, 11)}]},
        "C": {"bom_id": 1, "hours": {"WC": 1.0},
              "details": [{"mo": {"id": 3}, "demand": 6,
                           "start": date(2026, 11, 11), "due": date(2026, 11, 11)}]},
    }
    with pytest.raises(ManufacturingPlanError, match="manufacturing_time_infeasible"):
        manufacturing._check_cumulative_capacity(
            object(), entries, exclude_mo_ids=set())

"""R01: references resolve to real records and impact is evidence-based."""
from __future__ import annotations

from forgeflow.integrations.odoo20.resolver import (
    DIRECT,
    POTENTIAL,
    UNCERTAIN,
    BusinessObjectResolver,
    split_reference,
)


class StubAdapter:
    """Minimal Odoo-shaped reader: enough to exercise resolution and impact."""

    def __init__(self, data: dict[str, list[dict]]) -> None:
        self.data = data
        self.calls: list[tuple[str, list]] = []

    def search_read(self, model: str, domain: list, fields: list[str],
                    limit: int = 100) -> list[dict]:
        self.calls.append((model, domain))
        rows = self.data.get(model, [])
        for condition in domain:
            field, operator, value = condition
            rows = [row for row in rows if _match(row, field, operator, value)]
        out = []
        for row in rows[:limit]:
            out.append({name: row.get(name) for name in fields if name in row})
        return out


def _match(row: dict, field: str, operator: str, value) -> bool:
    current = row
    for part in field.split("."):
        if not isinstance(current, dict):
            return False
        current = current.get(part)
    # Odoo returns Many2one as [id, display]; domains compare against the id.
    if isinstance(current, (list, tuple)) and len(current) == 2:
        current = current[0]
    if operator == "=":
        return current == value
    if operator == "=ilike":
        return str(current or "").lower() == str(value).lower()
    if operator == "in":
        return current in value
    if operator == "not in":
        return current not in value
    return False


def _adapter() -> StubAdapter:
    return StubAdapter({
        # The record named PO-123 has id 900: the digits are not the id.
        "purchase.order": [
            {"id": 900, "name": "PO-123", "display_name": "PO-123",
             "state": "purchase", "write_date": "2026-11-08 09:00:00",
             "company_id": [1, "HQ"], "partner_id": [7, "Supplier A"]},
            # Same name in another company.
            {"id": 901, "name": "PO-500", "display_name": "PO-500",
             "state": "purchase", "write_date": "2026-11-08 09:00:00",
             "company_id": [1, "HQ"], "partner_id": [7, "Supplier A"]},
            {"id": 902, "name": "PO-500", "display_name": "PO-500",
             "state": "purchase", "write_date": "2026-11-08 09:00:00",
             "company_id": [2, "Subsidiary"], "partner_id": [9, "Supplier B"]},
        ],
        "purchase.order.line": [
            {"id": 1, "order_id": [900, "PO-123"], "product_id": [50, "Widget"],
             "product_qty": 10.0},
        ],
        "stock.move": [
            # Only this move ties the purchase to a sale order.
            {"id": 70, "purchase_line_id": [1, "x"], "sale_line_id": [500, "SO-9"],
             "product_id": [50, "Widget"], "state": "assigned"},
        ],
        "sale.order.line": [
            {"id": 500, "order_id": [300, "SO-9"], "product_id": [50, "Widget"],
             "product_uom_qty": 10.0, "product_uom_id": [1, "Units"], "price_unit": 12.0},
            {"id": 501, "order_id": [301, "SO-10"], "product_id": [50, "Widget"],
             "product_uom_qty": 5.0, "product_uom_id": [1, "Units"], "price_unit": 12.0},
            {"id": 502, "order_id": [302, "SO-11"], "product_id": [50, "Widget"],
             "product_uom_qty": 2.0, "product_uom_id": [1, "Units"], "price_unit": 12.0},
        ],
        "stock.quant": [
            {"product_id": [50, "Widget"], "quantity": 40.0, "reserved_quantity": 5.0,
             "location_id": {"usage": "internal"}},
        ],
        # Widget is a component of Assembly, and an MO for Assembly needs Press.
        "mrp.bom.line": [
            {"id": 1, "bom_id": [400, "Assembly BOM"], "product_id": [50, "Widget"]},
        ],
        "mrp.bom": [
            {"id": 400, "product_id": [60, "Assembly"],
             "product_tmpl_id": [60, "Assembly"]},
        ],
        "mrp.production": [
            {"id": 700, "name": "MO-1", "product_id": [60, "Assembly"],
             "product_qty": 5.0, "state": "confirmed",
             "date_start": "2026-11-10 08:00:00",
             "date_finished": "2026-11-12 08:00:00"},
        ],
        "mrp.workorder": [
            {"id": 800, "production_id": [700, "MO-1"],
             "workcenter_id": [900, "Press"], "duration_expected": 120.0,
             "state": "pending"},
        ],
        "mrp.workcenter": [
            {"id": 900, "name": "Press", "time_efficiency": 100.0},
        ],
        "mrp.workcenter.capacity": [
            {"id": 1, "workcenter_id": [900, "Press"], "product_id": [50, "Widget"],
             "capacity": 8.0},
        ],
    })


def test_reference_prefixes_are_typed_and_bounded() -> None:
    assert split_reference("PO-123") == ("purchase.order", "PO-123")
    assert split_reference("so 7") == (None, "so 7") or split_reference("so 7")[1] == "so 7"
    assert split_reference("SOP-1")[0] is None
    assert split_reference("mrp.production:12") == ("mrp.production", "12")
    assert split_reference("unknown.model:1")[0] is None
    assert split_reference("purchase.order:PO-9") == ("purchase.order", "PO-9")


def test_a_document_number_is_not_a_database_id() -> None:
    resolver = BusinessObjectResolver(_adapter())
    resolution = resolver.resolve("PO-123")
    assert resolution.resolved is not None
    assert resolution.resolved.id == 900
    assert resolution.resolved.ref == "purchase.order:900"
    assert resolution.resolved.matched_by == "name"
    assert not resolution.ambiguous


def test_same_name_in_two_companies_is_ambiguous_then_scoped() -> None:
    resolver = BusinessObjectResolver(_adapter())
    both = resolver.resolve("PO-500")
    assert both.ambiguous is True
    assert both.resolved is None

    adapter = _adapter()
    scoped = BusinessObjectResolver(adapter, company_ids=(1,))
    one = scoped.resolve("PO-500")
    assert one.resolved is not None and one.resolved.id == 901
    # The other company's row is never read: scope is applied in the query, not
    # by reading a candidate the tenant may not see and filtering it afterwards.
    assert one.rejected == []
    purchase_calls = [domain for model, domain in adapter.calls
                      if model == "purchase.order"]
    assert purchase_calls
    assert all(("company_id", "in", [1]) in domain for domain in purchase_calls)


def test_a_supplier_is_not_used_as_a_customer() -> None:
    resolver = BusinessObjectResolver(_adapter())
    impact = resolver.impact("PO-123")
    # SO-9 is direct because a stock move links it, not because a partner matches.
    assert [item["ref"] for item in impact["direct"]] == ["sale.order:300"]
    assert impact["direct"][0]["path"][0] == "purchase.order:900"
    for item in impact["direct"] + impact["potential"] + impact["uncertain"]:
        assert item["strength"] in {DIRECT, POTENTIAL, UNCERTAIN}


def test_a_shared_product_alone_is_not_a_direct_impact() -> None:
    resolver = BusinessObjectResolver(_adapter())
    impact = resolver.impact("PO-123")
    direct = {item["ref"] for item in impact["direct"]}
    others = {item["ref"] for item in impact["potential"] + impact["uncertain"]}
    assert "sale.order:301" not in direct
    # 35 available covers SO-10 (5) and SO-11 (2), so neither is claimed as a
    # supply conflict; they are reported as uncertain, with the reason.
    assert "sale.order:301" in others
    assert all(item["reason"] for item in impact["uncertain"])


def test_an_unknown_reference_returns_no_invented_id() -> None:
    resolver = BusinessObjectResolver(_adapter())
    impact = resolver.impact("PO-999")
    assert impact["resolved"] is None
    assert impact["direct"] == [] and impact["potential"] == [] and impact["uncertain"] == []
    assert impact["candidates"] == []


def test_impact_records_where_and_when() -> None:
    resolver = BusinessObjectResolver(_adapter())
    impact = resolver.impact("PO-123")
    assert impact["observed_at"]
    assert impact["truncated"] is False
    assert impact["resolved"]["company_id"] == 1


def test_a_supplier_is_not_a_customer_and_bom_demand_is_traced() -> None:
    resolver = BusinessObjectResolver(_adapter())
    impact = resolver.impact("PO-123")
    # Direct demand comes from a stock move that links the purchase line to the
    # sale line; the supplier partner id is never used as a customer id.
    assert [item["ref"] for item in impact["direct"]] == ["sale.order:300"]
    manufacturing = {item["ref"]: item for item in impact["manufacturing"]}
    # Widget is a component of Assembly, so the Assembly MO is a real demand.
    assert manufacturing["mrp.production:700"]["via"] == "bom_component_parent"
    centers = {item["workcenter"]: item for item in impact["workcenters"]}
    assert centers["Press"]["capacity"] == 8.0
    assert impact["completeness"]["complete"] is True


def test_a_failed_relation_query_is_unknown_not_no_impact() -> None:
    class Failing(StubAdapter):
        def search_read(self, model, domain, fields, limit=100):
            if model == "mrp.bom.line":
                raise RuntimeError("odoo_unavailable")
            return super().search_read(model, domain, fields, limit=limit)

    adapter = Failing(_adapter().data)
    impact = BusinessObjectResolver(adapter).impact("PO-123")
    assert impact["completeness"]["complete"] is False
    assert "mrp.bom.line" in impact["completeness"]["query_failures"]
    assert impact["completeness"]["reason"] == "partial"


def test_snapshot_records_provenance_units_and_completeness() -> None:
    resolver = BusinessObjectResolver(_adapter())
    snapshot = resolver.snapshot("PO-123")
    assert snapshot["object_ref"] == "purchase.order:900"
    record = snapshot["record"]
    assert record["model"] == "purchase.order"
    assert record["id"] == 900
    assert record["business_version"] == "2026-11-08 09:00:00"
    assert snapshot["source"]["observed_at"]
    assert snapshot["completeness"]["complete"] is True
    assert snapshot["relations"]["direct"][0]["unit"]
    assert snapshot["relations"]["direct"][0]["move_state"] == "assigned"


def test_an_ambiguous_reference_has_no_snapshot_claims() -> None:
    resolver = BusinessObjectResolver(_adapter())
    snapshot = resolver.snapshot("PO-500")
    assert snapshot["record"] is None
    assert snapshot["completeness"]["complete"] is False
    assert snapshot["completeness"]["reason"] == "ambiguous_reference"
    assert len(snapshot["candidates"]) == 2

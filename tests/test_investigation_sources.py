"""Bounded business reads and public policy projection, without hidden answers."""

import copy
import re

import pytest

from forgeflow.contract import build_contract
from forgeflow.investigation.sources import (
    MODELS,
    RAW_FIELDS,
    FixtureSource,
    OdooSource,
    SourceError,
)


def contract():
    return build_contract(
        contract_id="investigation-v1", task_id="supplier-case",
        task_type="supplier_cancellation", product_code="FIN", anchor_date="2026-01-01",
        margin=0.2, margin_basis="new_spend", budgets={"30": 1000}, sales_order_ids=[30],
        disruption={"kind": "supplier_cancellation", "vendor_ref": "V-1", "vendor_name": "A"},
        original_commitment_policy="repair_plan", sources={"policy": "task-public-policy"},
    )


def record(entity, identity, **fields):
    return {"entity": entity, "id": str(identity), "fields": fields}


def records():
    return [
        record("supplier", 1, name="Supplier A", ref="V-1"),
        record("supplier", 2, name="Supplier A East", ref="V-2"),
        record("product", 10, name="Finished item", default_code="FIN", product_template_id="100"),
        record("product", 11, name="Unrelated item", default_code="OTHER", product_template_id="101"),
        record("purchase_order", 20, name="PO-20", state="purchase", supplier_id="1",
               origin="SO-30"),
        record("purchase_order", 21, name="PO-21", state="cancel", supplier_id="2",
               origin="SO-31"),
        record("sales_order", 30, name="SO-30", state="sale", customer_id="9",
               commitment_date="2026-01-10 12:00:00", client_order_ref="Customer request"),
        record("sales_order", 31, name="SO-31", state="done", customer_id="8",
               commitment_date="2026-01-11 12:00:00", client_order_ref=""),
        record("sales_order", 32, name="SO-32", state="sale", customer_id="7",
               commitment_date=None, client_order_ref=""),
        record("purchase_order_line", 200, order_id="20", product_id="10", quantity=5,
               unit_price=60, received_quantity=0, planned_date="2026-01-02 10:00:00"),
        record("sales_order_line", 300, order_id="30", product_id="10", quantity=10,
               unit_price=100, delivered_quantity=0),
        record("sales_order_line", 301, order_id="31", product_id="10", quantity=10,
               unit_price=100, delivered_quantity=2),
        record("sales_order_line", 302, order_id="32", product_id="10", quantity=10,
               unit_price=100, delivered_quantity=10),
        record("sales_order_line", 399, order_id="32", product_id="11", quantity=5,
               unit_price=100, delivered_quantity=0),
        record("supplier_offer", 1000, supplier_id="1", supplier_ref="V-1", product_id="10",
               product_template_id="100", product_code="FIN", unit_price=60.0,
               lead_days=1, minimum_quantity=1, offer_key="V-1|FIN|1|60.0|1"),
    ]


def raw_records():
    return {
        "res.partner": [
            {"id": 1, "name": "Supplier A", "ref": "V-1", "supplier_rank": 1},
            {"id": 2, "name": "Supplier A East", "ref": "V-2", "supplier_rank": 1},
            {"id": 9, "name": "Customer", "ref": "C-9", "supplier_rank": 0},
        ],
        "product.product": [
            {"id": 10, "name": "Finished item", "default_code": "FIN", "product_tmpl_id": [100, "T"]},
            {"id": 11, "name": "Unrelated item", "default_code": "OTHER", "product_tmpl_id": [101, "U"]},
        ],
        "purchase.order": [
            {"id": 20, "name": "PO-20", "state": "purchase", "partner_id": [1, "Supplier A"],
             "origin": "SO-30"},
            {"id": 21, "name": "PO-21", "state": "cancel", "partner_id": [2, "Supplier A East"],
             "origin": "SO-31"},
        ],
        "sale.order": [
            {"id": 30, "name": "SO-30", "state": "sale", "partner_id": [9, "Customer"],
             "commitment_date": "2026-01-10 12:00:00", "client_order_ref": "Customer request"},
            {"id": 31, "name": "SO-31", "state": "done", "partner_id": [8, "C8"],
             "commitment_date": "2026-01-11 12:00:00", "client_order_ref": False},
            {"id": 32, "name": "SO-32", "state": "sale", "partner_id": [7, "C7"],
             "commitment_date": False, "client_order_ref": False},
        ],
        "purchase.order.line": [
            {"id": 200, "order_id": [20, "PO-20"], "product_id": [10, "FIN"], "product_qty": 5,
             "price_unit": 60, "qty_received": 0, "date_planned": "2026-01-02 10:00:00",
             "display_type": False},
        ],
        "sale.order.line": [
            {"id": identity, "order_id": [order, "SO"], "product_id": [product, "Product"],
             "product_uom_qty": qty, "price_unit": 100, "qty_delivered": delivered,
             "display_type": False}
            for identity, order, product, qty, delivered in [
                (300, 30, 10, 10, 0), (301, 31, 10, 10, 2), (302, 32, 10, 10, 10),
                (399, 32, 11, 5, 0),
            ]
        ],
        "product.supplierinfo": [
            {"id": 1000, "partner_id": [1, "Supplier A"], "product_id": False,
             "product_tmpl_id": [100, "T"], "price": 60, "delay": 1, "min_qty": 1},
        ],
        "mrp.bom": [],
        "mrp.bom.line": [],
    }


class ReadOnlyConnection:
    def __init__(self):
        self.models = raw_records()
        self.calls = []
        self.reverse_read = False
        self.extra_search_id = False

    def get_model(self, name):
        assert name in MODELS.values()
        owner = self

        class Model:
            def fields_get(self, *, allfields, attributes):
                owner.calls.append((name, "fields_get", {"fields": allfields}))
                expected = RAW_FIELDS[next(key for key, value in MODELS.items() if value == name)]
                assert set(allfields) == set(expected)
                return {field: {"type": "any"} for field in expected}

            def search(self, *, domain, limit, offset, order):
                owner.calls.append((name, "search", {
                    "domain": domain, "limit": limit, "offset": offset, "order": order,
                }))
                assert domain and 1 <= limit <= 51 and order == "id asc"

                def matches(row):
                    tokens = iter(domain)

                    def expression():
                        item = next(tokens)
                        if item == "|":
                            left, right = expression(), expression()
                            return left or right
                        if item == "&":
                            left, right = expression(), expression()
                            return left and right
                        field, op, expected = item
                        actual = row[field]
                        if isinstance(actual, list):
                            actual = actual[0]
                        if op == "=":
                            return actual == expected
                        if op == "in":
                            return actual in expected
                        if op == ">":
                            return actual > expected
                        assert op == "ilike"
                        literal = re.sub(r"\\(.)", r"\1", expected)
                        return literal.casefold() in str(actual).casefold()

                    outcomes = []
                    while True:
                        try:
                            outcomes.append(expression())
                        except StopIteration:
                            return all(outcomes)

                ids = sorted(row["id"] for row in owner.models[name] if matches(row))
                result = ids[offset:offset + limit]
                if owner.extra_search_id:
                    result += list(range(9000, 9000 + limit))
                return result

            def read(self, *, ids, fields):
                owner.calls.append((name, "read", {"ids": ids, "fields": fields}))
                rows = [{field: row[field] for field in fields}
                        for row in owner.models[name] if row["id"] in ids]
                if owner.reverse_read:
                    rows.reverse()
                return rows[0] if len(rows) == 1 else rows

        return Model()


def pages(source, kind, arguments):
    result, cursor = [], None
    for _ in range(30):
        page = source.query(kind, {**arguments, "cursor": cursor})
        assert set(page) == {"records", "complete", "next_cursor"}
        assert len(page["records"]) <= arguments["limit"]
        result.extend(page["records"])
        if page["complete"]:
            assert page["next_cursor"] is None
            return result
        assert page["next_cursor"] and page["next_cursor"] != cursor
        cursor = page["next_cursor"]
    pytest.fail("pagination did not finish")


@pytest.mark.parametrize("kind,args", [
    ("entities", {"entity": "supplier", "query": "Supplier A", "limit": 1}),
    ("entities", {"entity": "product", "query": "FIN", "limit": 1}),
    ("entities", {"entity": "purchase_order", "query": "20", "limit": 1}),
    ("entities", {"entity": "sales_order", "query": "SO-3", "limit": 1}),
    ("relations", {"entity": "supplier", "id": "1", "limit": 1}),
    ("relations", {"entity": "product", "id": "10", "limit": 2}),
    ("relations", {"entity": "purchase_order", "id": "20", "limit": 1}),
    ("relations", {"entity": "sales_order", "id": "30", "limit": 1}),
])
def test_fixture_and_odoo_have_the_same_targeted_paginated_results(kind, args):
    fixture = FixtureSource(records(), contract())
    connection = ReadOnlyConnection()
    live = OdooSource(connection, contract())
    assert pages(live, kind, args) == pages(fixture, kind, args)
    assert {operation for _, operation, _ in connection.calls} <= {"fields_get", "search", "read"}


def test_product_relations_do_not_drop_done_or_delivered_demand_or_leak_other_products():
    connection = ReadOnlyConnection()
    source = OdooSource(connection, contract())
    related = pages(source, "relations", {"entity": "product", "id": 10, "limit": 1})
    lines = [row for row in related if row["entity"] == "sales_order_line"]
    assert [row["id"] for row in lines] == ["300", "301", "302"]
    assert [row["fields"]["delivered_quantity"] for row in lines] == [0, 2, 10]
    sales_reads = [args["ids"] for model, operation, args in connection.calls
                   if model == "sale.order.line" and operation == "read"]
    assert all(399 not in ids for ids in sales_reads)


@pytest.mark.parametrize("factory", [
    lambda: FixtureSource(records(), contract()),
    lambda: OdooSource(ReadOnlyConnection(), contract()),
])
def test_policy_does_not_disclose_scope_product_or_disruption(factory):
    source = factory()
    public = source.query("policy", {})["records"][0]
    assert public == {
        "entity": "policy", "id": "investigation-v1",
        "fields": {"task_type": "supplier_cancellation", "anchor_date": "2026-01-01",
                   "margin": 0.2, "margin_basis": "new_spend", "time_granularity": "day"},
    }
    authorized = source.query("policy", {"order_id": "30"})["records"][0]
    assert authorized["fields"] == {"authorized": True, "budget_cap": 1000}
    assert source.query("policy", {"order_id": "31"})["records"][0]["fields"] == {
        "authorized": False, "budget_cap": None,
    }
    assert source.query("record", {"entity": "policy", "id": public["id"]})["records"] == [public]
    assert source.query("record", {"entity": "order_policy", "id": "30"})["records"] == [authorized]


def test_fixture_policy_distinguishes_missing_budget_from_missing_authorization():
    policy = {"contract_id": "fixture-policy", "task_type": "supplier_cancellation",
              "anchor_date": "2026-01-01", "margin": 0.2, "margin_basis": "new_spend",
              "time_granularity": "day", "orders": {"30": {"authorized": True, "budget_cap": None}}}
    source = FixtureSource(records(), policy)
    assert source.query("policy", {"order_id": 30})["records"][0]["fields"] == {
        "authorized": True, "budget_cap": None,
    }
    policy["hidden_reference"] = {"sales_order_ids": [30]}
    with pytest.raises(SourceError, match="invalid_source_policy"):
        FixtureSource(records(), policy)


def test_record_reads_live_values_again_and_sorts_unordered_read_results():
    connection = ReadOnlyConnection()
    source = OdooSource(connection, contract())
    assert source.query("record", {"entity": "supplier", "id": 1})["records"][0]["fields"]["ref"] == "V-1"
    connection.models["res.partner"][0]["ref"] = "V-CHANGED"
    assert source.query("record", {"entity": "supplier", "id": 1})["records"][0]["fields"]["ref"] == "V-CHANGED"
    connection.reverse_read = True
    rows = source.query("entities", {"entity": "supplier", "query": "Supplier"})["records"]
    assert [row["id"] for row in rows] == ["1", "2"]
    assert source.query("record", {"entity": "sales_order", "id": 999})["records"] == []


def test_offer_key_uses_live_supplier_product_price_and_does_not_guess_variant():
    connection = ReadOnlyConnection()
    source = OdooSource(connection, contract())
    query = {"entity": "supplier_offer", "id": 1000}
    first = source.query("record", query)["records"][0]
    assert first["fields"]["offer_key"] == "V-1|FIN|1|60.0|1"
    connection.models["product.product"].append({
        "id": 12, "name": "Other variant", "default_code": "FIN-B", "product_tmpl_id": [100, "T"],
    })
    ambiguous = source.query("record", query)["records"][0]["fields"]
    assert ambiguous["product_id"] is None
    assert ambiguous["product_code"] is None
    assert ambiguous["offer_key"] is None


def test_entity_queries_treat_sku_wildcards_as_literal_text():
    connection = ReadOnlyConnection()
    connection.models["product.product"][0]["default_code"] = "FIN_100%"
    live = OdooSource(connection, contract())
    fixture_records = records()
    fixture_records[2]["fields"]["default_code"] = "FIN_100%"
    fixture = FixtureSource(fixture_records, contract())
    args = {"entity": "product", "query": "FIN_100%"}
    assert live.query("entities", args) == fixture.query("entities", args)
    searches = [args for model, op, args in connection.calls if op == "search"]
    assert ("default_code", "ilike", r"FIN\_100\%") in searches[0]["domain"]


@pytest.mark.parametrize("kind,args", [
    ("write", {}),
    ("entities", {"entity": "res.users", "query": "all"}),
    ("entities", {"entity": "bom", "query": "all"}),
    ("entities", {"entity": "bom_line", "query": "all"}),
    ("relations", {"entity": "bom_line", "id": "5000"}),
    ("entities", {"entity": "supplier", "query": ""}),
    ("entities", {"entity": "supplier", "query": "A", "limit": 51}),
    ("entities", {"entity": "supplier", "query": "A", "limit": True}),
    ("entities", {"entity": "supplier", "query": "A", "domain": []}),
    ("relations", {"entity": "product", "id": "10", "fields": ["secret"]}),
    ("record", {"entity": "product", "id": "../secret"}),
    ("record", {"entity": "product", "id": True}),
    ("policy", {"order_ids": [30]}),
])
def test_invalid_or_unbounded_queries_are_rejected_before_rpc(kind, args):
    connection = ReadOnlyConnection()
    source = OdooSource(connection, contract())
    with pytest.raises(SourceError):
        source.query(kind, args)
    assert connection.calls == []


def test_cursor_is_bound_to_the_query_and_rejected_before_rpc():
    connection = ReadOnlyConnection()
    source = OdooSource(connection, contract())
    page = source.query("entities", {"entity": "supplier", "query": "Supplier", "limit": 1})
    connection.calls.clear()
    with pytest.raises(SourceError):
        source.query("relations", {"entity": "product", "id": 10, "cursor": page["next_cursor"]})
    assert connection.calls == []


def test_bad_odoo_response_cannot_claim_completeness_or_dump_records():
    connection = ReadOnlyConnection()
    connection.extra_search_id = True
    source = OdooSource(connection, contract())
    with pytest.raises(SourceError, match="invalid_source_response"):
        source.query("entities", {"entity": "supplier", "query": "Supplier", "limit": 1})
    assert not any(operation == "read" for _, operation, _ in connection.calls)


def test_fixture_rejects_unknown_fields_and_owns_copies():
    original = records()
    source = FixtureSource(original, contract())
    original[0]["fields"]["ref"] = "changed-outside"
    row = source.query("record", {"entity": "supplier", "id": 1})["records"][0]
    assert row["fields"]["ref"] == "V-1"
    row["fields"]["ref"] = "changed-output"
    assert source.query("record", {"entity": "supplier", "id": 1})["records"][0]["fields"]["ref"] == "V-1"
    polluted = copy.deepcopy(original)
    polluted[0]["fields"]["reference_request"] = {"sales_order_ids": [30]}
    with pytest.raises(SourceError, match="invalid_source_record"):
        FixtureSource(polluted, contract())


def bom_sources():
    """The same component graph in normalized records and raw Odoo relations."""
    rows = records()
    connection = ReadOnlyConnection()
    # Keep this explicit variant offer distinct from the generic template BoM.
    connection.models["product.supplierinfo"][0]["product_id"] = [10, "Finished item"]
    for identity, name, code, template in [(12, "Raw component", "RAW", 102),
                                           (13, "Second variant", "FIN-B", 100)]:
        rows.append(record("product", identity, name=name, default_code=code,
                           product_template_id=str(template)))
        connection.models["product.product"].append({
            "id": identity, "name": name, "default_code": code, "product_tmpl_id": [template, "T"],
        })
    for identity, product, template, active in [(500, None, 100, True), (501, 10, 100, False),
                                                (502, 13, 100, True), (503, 11, 101, True)]:
        rows.append(record("bom", identity, product_id=str(product) if product else None,
                           product_template_id=str(template), quantity=2, type="normal", active=active))
        connection.models["mrp.bom"].append({
            "id": identity, "product_id": [product, "P"] if product else False,
            "product_tmpl_id": [template, "T"], "product_qty": 2, "type": "normal", "active": active,
        })
    for identity, bom, component, quantity in [(5000, 500, 12, 4), (5001, 503, 12, 1),
                                               (5002, 501, 11, 2)]:
        rows.append(record("bom_line", identity, bom_id=str(bom), product_id=str(component), quantity=quantity))
        connection.models["mrp.bom.line"].append({
            "id": identity, "bom_id": [bom, "BoM"], "product_id": [component, "Component"],
            "product_qty": quantity,
        })
    return FixtureSource(rows, contract()), OdooSource(connection, contract()), connection


@pytest.mark.parametrize("entity,identity", [("product", 10), ("product", 12), ("bom", 500), ("bom", 502)])
def test_component_relations_match_odoo_and_fixture_with_bounded_pagination(entity, identity):
    fixture, live, connection = bom_sources()
    arguments = {"entity": entity, "id": identity, "limit": 1}
    assert pages(live, "relations", arguments) == pages(fixture, "relations", arguments)
    assert {operation for _, operation, _ in connection.calls} <= {"fields_get", "search", "read"}
    for model, operation, args in connection.calls:
        if model == "mrp.bom.line" and operation == "search":
            assert all(field != "display_type" for field, _, _ in args["domain"])


def test_bom_graph_preserves_active_scope_and_template_ambiguity():
    fixture, live, _ = bom_sources()
    for source in (fixture, live):
        product = pages(source, "relations", {"entity": "product", "id": "10", "limit": 2})
        assert [row["id"] for row in product if row["entity"] == "bom"] == ["500"]
        component = pages(source, "relations", {"entity": "product", "id": "12", "limit": 2})
        assert [(row["id"], row["fields"]["bom_id"]) for row in component
                if row["entity"] == "bom_line"] == [("5000", "500"), ("5001", "503")]
        generic = source.query("record", {"entity": "bom", "id": "500"})["records"][0]
        assert generic["fields"]["product_id"] is None
        parents = pages(source, "relations", {"entity": "bom", "id": "500", "limit": 1})
        assert [row["id"] for row in parents if row["entity"] == "product"] == ["10", "13"]
        specific = pages(source, "relations", {"entity": "bom", "id": "502", "limit": 1})
        assert [row["id"] for row in specific if row["entity"] == "product"] == ["13"]


@pytest.mark.parametrize("entity,identity", [("bom", 500), ("bom", 501), ("bom_line", 5000)])
def test_bom_record_preserves_the_actual_business_fields(entity, identity):
    fixture, live, _ = bom_sources()
    args = {"entity": entity, "id": identity}
    assert live.query("record", args) == fixture.query("record", args)


@pytest.mark.parametrize("field,value", [("active", 1), ("quantity", 0), ("quantity", -1),
                                         ("product_template_id", None)])
def test_invalid_bom_data_is_rejected_in_both_sources(field, value):
    fixture, live, connection = bom_sources()
    next(row for row in fixture.records if row["entity"] == "bom")["fields"][field] = value
    raw_field = {"quantity": "product_qty", "product_template_id": "product_tmpl_id"}.get(field, field)
    connection.models["mrp.bom"][0][raw_field] = value
    for source in (fixture, live):
        with pytest.raises(SourceError):
            source.query("record", {"entity": "bom", "id": 500})

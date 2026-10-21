"""Regression checks for API behaviors observed in the pinned Odoo fixture."""
from __future__ import annotations

import copy

import pytest

from forgeflow.recovery.odoo import DocumentError, PlanDocuments


class RecordingConnection:
    def __init__(self, result=None, fail_at=None):
        self.result = [42] if result is None else result
        self.calls = []
        self.fail_at = fail_at

    def get_model(self, model):
        parent = self

        class Proxy:
            def __getattr__(self, method):
                def invoke(**kwargs):
                    parent.calls.append((model, method, kwargs))
                    if len(parent.calls) == parent.fail_at:
                        raise TimeoutError("private transport data")
                    return parent.result
                return invoke
        return Proxy()


def create_mo(documents, **overrides):
    args = dict(name="FF/MO/synthetic", company_id=1, product_id=2, product_uom_id=1,
                bom_id=1, quantity=3, consumer_names=["SO001"],
                start="2026-10-22 00:00:00", deadline="2026-10-28 00:00:00")
    return documents.create_manufacturing(**(args | overrides))


def test_singleton_create_response_returns_real_id_once():
    connection = RecordingConnection()
    assert create_mo(PlanDocuments(connection)) == 42
    assert len(connection.calls) == 1


@pytest.mark.parametrize("result", [42, [], [1, 2], [True], ["42"]])
def test_unexpected_create_response_never_issues_another_create(result):
    connection = RecordingConnection(result=result)
    with pytest.raises(DocumentError):
        create_mo(PlanDocuments(connection))
    assert len(connection.calls) == 1


def test_quantity_change_uses_native_wizard_not_mo_header_only():
    connection = RecordingConnection()
    PlanDocuments(connection).set_manufacturing_quantity(7, 3)
    assert connection.calls == [
        ("change.production.qty", "create", {"vals_list": [{"mo_id": 7, "product_qty": 3}]}),
        ("change.production.qty", "change_prod_qty", {"ids": [42]}),
    ]


def test_origin_only_update_does_not_write_unchanged_dates_or_unplan_workorders():
    connection = RecordingConnection()
    PlanDocuments(connection).set_manufacturing_origin(7, consumer_names=["SO002", "SO001"])
    assert connection.calls == [
        ("mrp.production", "write", {"ids": [7], "vals": {"origin": "SO001, SO002"}}),
    ]


def test_center_recompute_finishes_before_writing_explicit_schedule():
    connection = RecordingConnection()
    PlanDocuments(connection).set_workorder_plan(
        9, workcenter_id=3, start="2026-10-22 08:00:00", finish="2026-10-22 10:32:00",
        duration_minutes=152.0,
    )
    assert connection.calls == [
        ("mrp.workorder", "write", {"ids": [9], "vals": {"workcenter_id": 3}}),
        ("mrp.workorder", "write", {"ids": [9], "vals": {
            "date_start": "2026-10-22 08:00:00", "date_finished": "2026-10-22 10:32:00",
            "duration_expected": 152.0,
        }}),
    ]


@pytest.mark.parametrize("fail_at", [1, 2])
def test_uncertain_schedule_write_has_no_internal_retry_or_error_disclosure(fail_at):
    connection = RecordingConnection(fail_at=fail_at)
    with pytest.raises(DocumentError, match="^remote_operation_outcome_unknown$"):
        PlanDocuments(connection).set_workorder_plan(
            9, workcenter_id=3, start="2026-10-22 08:00:00", finish="2026-10-22 10:32:00",
            duration_minutes=152.0,
        )
    assert len(connection.calls) == fail_at


@pytest.mark.parametrize("overrides", [
    {"quantity": 0}, {"quantity": True}, {"product_id": -1},
    {"consumer_names": ["SO001", "SO001"]}, {"consumer_names": [["SO001"]]},
    {"consumer_names": ["SO001, SO002"]}, {"deadline": "not a date"},
    {"deadline": "2026-10-21 00:00:00"},
])
def test_invalid_inputs_fail_before_any_remote_mutation(overrides):
    connection = RecordingConnection()
    with pytest.raises(DocumentError):
        create_mo(PlanDocuments(connection), **overrides)
    assert connection.calls == []


class ReceiptConnection:
    """Observed pinned-Odoo behavior: a POL write changes only the deadline."""

    def __init__(self, *, fail_write=None):
        original = "2026-10-26 03:43:07"
        self.db = {
            "purchase.order": {1: {"id": 1, "state": "purchase", "company_id": [1, "Co"],
                                    "order_line": [11], "picking_ids": [31]}},
            "purchase.order.line": {11: {
                "id": 11, "order_id": [1, "PO"], "product_id": [2, "Component"],
                "product_uom_id": [1, "Units"], "product_qty": 10, "qty_received": 0,
                "price_unit": 2.0, "date_planned": original, "display_type": False,
                "move_ids": [21],
            }},
            "stock.move": {21: {
                "id": 21, "purchase_line_id": [11, "Line"], "picking_id": [31, "Receipt"],
                "product_id": [2, "Component"], "product_uom": [1, "Units"],
                "product_uom_qty": 10, "company_id": [1, "Co"],
                "raw_material_production_id": False, "production_id": False,
                "state": "assigned", "picked": False, "date": original,
                "date_deadline": original, "location_id": 8, "location_dest_id": 12,
            }},
            "stock.picking": {31: {
                "id": 31, "move_ids": [21], "state": "assigned", "date_done": False,
                "scheduled_date": original,
            }},
        }
        # Unrelated records must remain untouched even when they share dates.
        self.db["stock.move"][22] = {"id": 22, "date": original, "date_deadline": original}
        self.db["stock.picking"][32] = {"id": 32, "scheduled_date": original}
        self.writes = []
        self.fail_write = fail_write

    def get_model(self, model):
        parent = self

        class Proxy:
            def read(self, *, ids, fields):
                return [{key: copy.deepcopy(parent.db[model][i][key]) for key in fields}
                        for i in ids]

            def write(self, *, ids, vals):
                parent.writes.append((model, ids, copy.deepcopy(vals)))
                for i in ids:
                    parent.db[model][i].update(vals)
                    if model == "purchase.order.line":
                        parent.db["stock.move"][21].update(
                            product_uom_qty=vals["product_qty"],
                            date_deadline=vals["date_planned"],
                        )
                if len(parent.writes) == parent.fail_write:
                    raise TimeoutError("private authorization material")
                return True

        return Proxy()


ARRIVAL = "2026-10-30 00:00:00"


def test_changed_purchase_arrival_synchronizes_only_its_bound_unexecuted_receipt():
    connection = ReceiptConnection()
    before = copy.deepcopy(connection.db)
    documents = PlanDocuments(connection)
    documents.set_purchase_line(11, quantity=10, price_unit=2.0, arrival=ARRIVAL)
    assert documents.purchase_arrival_matches(11, ARRIVAL)
    assert connection.writes == [
        ("purchase.order.line", [11], {
            "product_qty": 10, "price_unit": 2.0, "date_planned": ARRIVAL,
        }),
        ("stock.picking", [31], {"scheduled_date": ARRIVAL}),
        ("stock.move", [21], {"date": ARRIVAL, "date_deadline": ARRIVAL}),
    ]
    expected = copy.deepcopy(before)
    expected["purchase.order.line"][11]["date_planned"] = ARRIVAL
    expected["stock.picking"][31]["scheduled_date"] = ARRIVAL
    expected["stock.move"][21].update(date=ARRIVAL, date_deadline=ARRIVAL)
    assert connection.db == expected


def test_unchanged_purchase_arrival_preserves_existing_receipt_dates():
    connection = ReceiptConnection()
    before = copy.deepcopy(connection.db)
    PlanDocuments(connection).set_purchase_line(
        11, quantity=10, price_unit=3.0,
        arrival=before["purchase.order.line"][11]["date_planned"],
    )
    assert len(connection.writes) == 1
    assert connection.db["stock.move"] == before["stock.move"]
    assert connection.db["stock.picking"] == before["stock.picking"]


@pytest.mark.parametrize("model,identity,field,value", [
    ("purchase.order.line", 11, "move_ids", [21, 22]),
    ("purchase.order.line", 11, "qty_received", 1),
    ("purchase.order", 1, "order_line", [11, 12]),
    ("purchase.order", 1, "picking_ids", [31, 32]),
    ("stock.move", 21, "purchase_line_id", [12, "Other"]),
    ("stock.move", 21, "picking_id", [32, "Other"]),
    ("stock.move", 21, "product_id", [3, "Other"]),
    ("stock.move", 21, "company_id", [2, "Other"]),
    ("stock.move", 21, "picked", True),
    ("stock.move", 21, "state", "done"),
    ("stock.move", 21, "production_id", [2, "MO"]),
    ("stock.picking", 31, "move_ids", [21, 22]),
    ("stock.picking", 31, "state", "done"),
    ("stock.picking", 31, "date_done", "2026-10-27 00:00:00"),
])
def test_purchase_arrival_rejects_split_shared_or_executed_receipt_before_write(
    model, identity, field, value,
):
    connection = ReceiptConnection()
    connection.db[model][identity][field] = value
    with pytest.raises(DocumentError):
        PlanDocuments(connection).set_purchase_line(11, quantity=10, price_unit=2.0, arrival=ARRIVAL)
    assert connection.writes == []


@pytest.mark.parametrize("fail_write", [1, 2, 3])
def test_purchase_receipt_response_loss_never_retries_and_readback_checks_actual_dates(fail_write):
    connection = ReceiptConnection(fail_write=fail_write)
    documents = PlanDocuments(connection)
    with pytest.raises(DocumentError, match="^remote_operation_outcome_unknown$"):
        documents.set_purchase_line(11, quantity=10, price_unit=2.0, arrival=ARRIVAL)
    assert len(connection.writes) == fail_write
    assert documents.purchase_arrival_matches(11, ARRIVAL) is (fail_write == 3)


@pytest.mark.parametrize("record", [
    {"id": 11, "move_ids": []}, [{"id": 11, "move_ids": []}],
])
def test_purchase_receipt_read_accepts_only_a_complete_single_record(record):
    connection = RecordingConnection(result=record)
    observed = PlanDocuments(connection)._read_record("purchase.order.line", 11, ["id", "move_ids"])
    assert observed == {"id": 11, "move_ids": []}
    assert [call[1] for call in connection.calls] == ["read"]


@pytest.mark.parametrize("record", [
    [], [{"id": 11, "move_ids": []}, {"id": 12, "move_ids": []}],
    {"id": 12, "move_ids": []}, {"id": True, "move_ids": []},
    {"id": 11.0, "move_ids": []}, {"id": "11", "move_ids": []},
    {"id": 11}, {"move_ids": []}, [{"id": 11}], ["record"], "record", 11, True,
])
def test_purchase_receipt_read_rejects_incomplete_wrong_id_or_wrong_shape(record):
    connection = RecordingConnection(result=record)
    with pytest.raises(DocumentError, match="^incomplete_purchase_receipt_read$"):
        PlanDocuments(connection)._read_record("purchase.order.line", 11, ["id", "move_ids"])
    assert [call[1] for call in connection.calls] == ["read"]


@pytest.mark.parametrize("change", ["date", "quantity"])
def test_purchase_update_uses_actual_json2_singleton_shape(monkeypatch, change):
    # Exercise the installed client, with HTTP replaced by a synthetic ERP.
    # The wire read returns a list; JsonModel.read must unwrap it before our
    # document adapter sees it. No network request or real credential is used.
    import httpx

    odoolib = pytest.importorskip("odoolib")
    server = ReceiptConnection()

    def post(url, *, headers, json, timeout):
        model, method = url.rsplit("/", 2)[-2:]
        assert method in {"read", "write"}
        result = getattr(server.get_model(model), method)(**json)
        return httpx.Response(200, json=result)

    monkeypatch.setattr(httpx, "post", post)
    connection = odoolib.get_connection(
        hostname="synthetic.invalid", protocol="json2", port=8069,
        database="synthetic", password="synthetic-test-value",
    )
    assert isinstance(connection.get_model("purchase.order.line").read(ids=[11], fields=["id"]), dict)
    before = copy.deepcopy(server.db)
    arrival = ARRIVAL if change == "date" else before["purchase.order.line"][11]["date_planned"]
    quantity = 11 if change == "quantity" else 10
    documents = PlanDocuments(connection)
    documents.set_purchase_line(11, quantity=quantity, price_unit=2.0, arrival=arrival)
    assert server.db["purchase.order.line"][11]["product_qty"] == quantity
    assert server.db["purchase.order.line"][11]["date_planned"] == arrival
    assert server.db["stock.move"][21]["product_uom_qty"] == quantity
    assert len(server.writes) == (3 if change == "date" else 1)
    assert server.db["stock.move"][22] == before["stock.move"][22]
    assert server.db["stock.picking"][32] == before["stock.picking"][32]
    if change == "date":
        assert documents.purchase_arrival_matches(11, arrival)
    else:
        assert server.db["stock.picking"] == before["stock.picking"]
        assert server.db["stock.move"][21]["date"] == before["stock.move"][21]["date"]

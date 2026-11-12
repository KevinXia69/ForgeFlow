"""Bounded, read-only business tables for investigation development controls.

This is not an Odoo simulator. It implements only the fields_get/search/read
subset used by OdooSource, collect_snapshot, and preparation baseline capture.
The trusted host owns the tables and contract; agents receive only the normal
investigation tools. No plans, expected scores, or hidden answers are loaded.
"""

from __future__ import annotations

import copy
import json
import re
from dataclasses import dataclass
from typing import Any

from forgeflow.contract import TaskContract, build_contract, contract_from_payload
from forgeflow.investigation.policy import PartialPolicy, policy_from_payload
from forgeflow.planning.live import FIELDS, MARKER
from forgeflow.recovery.scope_guard import EXTRA_FIELDS
from forgeflow.recovery.storage import digest

SCHEMAS = {
    model: frozenset(
        [item[0] if isinstance(item, tuple) else item for item in FIELDS.get(model, [])]
        + EXTRA_FIELDS.get(model, [])
    )
    for model in set(FIELDS) | set(EXTRA_FIELDS)
}
MAX_RECORDS = 10_000
DEVELOPMENT_DATASET_VERSION = "investigation-dev-v1"


class FixtureAccessError(ValueError):
    pass


def _need(condition: bool, reason: str = "unsupported_fixture_read") -> None:
    if not condition:
        raise FixtureAccessError(reason)


def _relation(value: Any) -> Any:
    if isinstance(value, (list, tuple)) and len(value) == 2 and isinstance(value[1], str):
        return value[0]
    return value


def _like(actual: Any, pattern: str) -> bool:
    _need(isinstance(pattern, str) and len(pattern) <= 512)
    pieces, escaped = [], False
    for char in pattern:
        if escaped:
            pieces.append(re.escape(char))
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "%":
            pieces.append(".*")
        elif char == "_":
            pieces.append(".")
        else:
            pieces.append(re.escape(char))
    if escaped:
        pieces.append(re.escape("\\"))
    return re.search("".join(pieces), str(actual or ""), re.IGNORECASE | re.DOTALL) is not None


def _predicate(domain: list, schema: frozenset):
    _need(isinstance(domain, list) and len(domain) <= 100)
    position = 0

    def parse():
        nonlocal position
        _need(position < len(domain))
        term = domain[position]
        position += 1
        if isinstance(term, str):
            _need(term in {"|", "&", "!"})
            first = parse()
            if term == "!":
                return lambda row: not first(row)
            second = parse()
            return ((lambda row: first(row) or second(row)) if term == "|"
                    else (lambda row: first(row) and second(row)))
        _need(isinstance(term, (tuple, list)) and len(term) == 3)
        field, op, expected = term
        _need(isinstance(field, str) and field in schema
              and isinstance(op, str) and op in {"=", "!=", "in", "not in", ">", "ilike"})
        if op in {"in", "not in"}:
            _need(isinstance(expected, (list, tuple)) and len(expected) <= MAX_RECORDS)
        if op == "ilike":
            _need(isinstance(expected, str) and len(expected) <= 512)

        def matches(row):
            _need(field in row, "fixture_field_missing")
            actual = _relation(row[field])
            if op == "=":
                return actual == expected
            if op == "!=":
                return actual != expected
            if op == "in":
                return actual in expected
            if op == "not in":
                return actual not in expected
            if op == "ilike":
                return _like(actual, expected)
            _need(type(actual) in (int, float) and type(expected) in (int, float))
            return actual > expected

        return matches

    expressions = []
    while position < len(domain):
        expressions.append(parse())
    return lambda row: all(expression(row) for expression in expressions)


class _Model:
    def __init__(self, connection, model):
        self._connection, self._model = connection, model

    def fields_get(self, *, allfields, attributes):
        _need(isinstance(allfields, list) and all(isinstance(field, str) for field in allfields)
              and isinstance(attributes, list) and set(attributes) <= {"type", "relation"})
        self._connection.calls.append({"model": self._model, "method": "fields_get",
                                       "fields": list(allfields)})
        return {field: {attribute: "any" if attribute == "type" else False
                        for attribute in attributes}
                for field in allfields if field in SCHEMAS[self._model]}

    def search(self, *, domain, limit=None, offset=0, order="id asc"):
        _need(order in {"id", "id asc"} and type(offset) is int and 0 <= offset <= MAX_RECORDS)
        _need(limit is None or type(limit) is int and 1 <= limit <= MAX_RECORDS)
        matches = _predicate(domain, SCHEMAS[self._model])
        self._connection.calls.append({"model": self._model, "method": "search",
                                       "domain": copy.deepcopy(domain), "limit": limit,
                                       "offset": offset, "order": order})
        ids = sorted(row["id"] for row in self._connection.records[self._model] if matches(row))
        return ids[offset:offset + limit] if limit is not None else ids[offset:]

    def read(self, *, ids, fields):
        _need(isinstance(ids, list) and len(ids) <= MAX_RECORDS
              and all(type(identity) is int and identity > 0 for identity in ids)
              and len(set(ids)) == len(ids))
        _need(isinstance(fields, list) and fields and all(isinstance(field, str) for field in fields)
              and set(fields) <= SCHEMAS[self._model])
        self._connection.calls.append({"model": self._model, "method": "read",
                                       "ids": list(ids), "fields": list(fields)})
        found = {row["id"]: row for row in self._connection.records[self._model]}
        rows = []
        for identity in ids:
            if identity in found:
                row = found[identity]
                _need(set(fields) <= set(row), "fixture_field_missing")
                rows.append({field: copy.deepcopy(row[field]) for field in fields})
        return rows

    def __getattr__(self, method):
        self._connection.write_attempts.append({"model": self._model, "method": method})
        raise FixtureAccessError("fixture_read_only")


class FixtureConnection:
    """The same raw records back investigation and the actual preparation code."""

    def __init__(self, records: dict[str, list[dict]]):
        _need(isinstance(records, dict) and set(records) <= set(SCHEMAS), "invalid_fixture_records")
        _need(all(isinstance(rows, list) for rows in records.values()), "invalid_fixture_records")
        _need(sum(map(len, records.values())) <= MAX_RECORDS, "fixture_record_limit")
        for model, rows in records.items():
            _need(all(isinstance(row, dict) and set(row) <= SCHEMAS[model]
                      and type(row.get("id")) is int and row["id"] > 0 for row in rows),
                  "invalid_fixture_records")
            _need(len({row["id"] for row in rows}) == len(rows), "duplicate_fixture_id")
        try:
            json.dumps(records, allow_nan=False)
        except (TypeError, ValueError):
            raise FixtureAccessError("invalid_fixture_records") from None
        self.records = {model: copy.deepcopy(records.get(model, [])) for model in SCHEMAS}
        self.calls: list[dict] = []
        self.write_attempts: list[dict] = []

    def get_model(self, model: str):
        _need(isinstance(model, str) and model in SCHEMAS, "unsupported_fixture_model")
        return _Model(self, model)

    def fingerprint(self) -> str:
        return digest(self.records)


@dataclass(frozen=True)
class FixtureCase:
    """Trusted control inputs; only event belongs in the agent's initial prompt."""

    case_id: str
    event: str
    records: dict[str, list[dict]]
    policy: TaskContract | PartialPolicy
    oracle: dict

    @property
    def contract(self) -> TaskContract | None:
        return self.policy if isinstance(self.policy, TaskContract) else None


def supplier_cancellation_control() -> FixtureCase:
    """One invented FIN -> RAW case, derived from the existing single-layer tests.

    Only records are supplied. The full solver, mapping, and baseline capture
    must produce their own artifacts. Ten units of canceled finished supply
    can be replaced by manufacturing or another supplier; no result is baked in.
    """
    records = {model: [] for model in SCHEMAS}

    def add(model, **values):
        row = {field: False for field in SCHEMAS[model]}
        row.update(values)
        records[model].append(row)
        return row

    for identity, code, price in [(1, "FIN", 100), (2, "RAW", 2)]:
        add("product.product", id=identity, name=code, default_code=code,
            product_tmpl_id=identity, list_price=price, standard_price=price / 2,
            type="consu", is_storable=True, route_ids=[], uom_id=1)
    add("res.currency", id=1, name="USD")
    add("res.partner", id=1, name="Fixture customer", ref="C1", email="", comment="", supplier_rank=0)
    for identity, name, ref, code in [
        (2, "Fixture component vendor", "V-RAW", "RAW"),
        (3, "Fixture old vendor", "V-OLD", "FIN"),
        (4, "Fixture alternate vendor", "V-NEW", "FIN"),
    ]:
        add("res.partner", id=identity, name=name, ref=ref, email="", supplier_rank=1,
            comment=f"Maximum order quantity for {code}: 100 units")
    add("sale.order", id=1, name="SO-1", partner_id=1, state="sale",
        commitment_date="2026-01-09 12:00:00", currency_id=1, company_id=1, client_order_ref="Fixture case")
    add("sale.order.line", id=1, order_id=1, product_id=1, product_uom_qty=10,
        price_unit=100, qty_delivered=0, discount=0, product_uom_id=1)
    add("mrp.bom", id=1, product_id=False, product_tmpl_id=1, product_qty=1,
        product_uom_id=1, type="normal", produce_delay=1, active=True)
    add("mrp.bom.line", id=1, bom_id=1, product_id=2, product_qty=2, product_uom_id=1)
    add("mrp.routing.workcenter", id=1, bom_id=1, name="Assembly", workcenter_id=1, sequence=1)
    for identity, code, capacity, cost in [(1, "WC-A", 100, 2), (2, "WC-B", 100, 3)]:
        note = (f"Benchmark capacity: {capacity} total processing minutes across the scenario horizon.\n"
                + MARKER + "\n" + json.dumps({"product_code": "FIN", "time_per_unit_minutes": cost,
                                              "unit_cost": cost, "lead_days": 1}))
        add("mrp.workcenter", id=identity, name=code, code=code, note=note,
            alternative_workcenter_ids=[2] if identity == 1 else [], time_efficiency=100,
            time_start=0, time_stop=0)
    for identity, vendor, product, price in [(1, 2, 2, 2), (2, 3, 1, 60), (3, 4, 1, 65)]:
        add("product.supplierinfo", id=identity, partner_id=vendor, product_id=False,
            product_tmpl_id=product, price=price, min_qty=1, delay=1, product_name="Standard",
            currency_id=1, company_id=1)
    arrival = "2026-01-03 12:00:00"
    add("purchase.order", id=1, name="PO-CANCEL", partner_id=3, state="purchase", origin="SO-1",
        currency_id=1, company_id=1, order_line=[1], picking_ids=[1])
    add("purchase.order.line", id=1, order_id=1, product_id=1, product_qty=10, price_unit=60,
        qty_received=0, date_planned=arrival, product_uom_id=1, move_ids=[1])
    add("stock.picking", id=1, state="assigned", origin="SO-1", scheduled_date=arrival,
        date_done=False, move_ids=[1])
    add("stock.move", id=1, product_id=1, product_uom=1, product_uom_qty=10, quantity=0,
        picked=False, state="assigned", date=arrival, date_deadline=arrival, picking_id=1,
        company_id=1, raw_material_production_id=False, production_id=False, purchase_line_id=1,
        location_id=2, location_dest_id=1)
    add("stock.location", id=1, usage="internal")
    add("stock.location", id=2, usage="supplier")
    for identity in (1, 2):
        add("stock.quant", id=identity, product_id=identity, location_id=1,
            quantity=0, reserved_quantity=0, company_id=1)
    contract = build_contract(
        contract_id="fixture-supplier-control-v1", task_id="fixture-supplier-control",
        task_type="supplier_cancellation", product_code="FIN", anchor_date="2026-01-01",
        margin=0.2, margin_basis="new_spend", budgets={"1": 2000}, sales_order_ids=[1],
        disruption={"kind": "supplier_cancellation", "vendor_ref": "V-OLD",
                    "vendor_name": "Fixture old vendor"},
        original_commitment_policy="repair_plan", sources={"policy": "synthetic-public-task-rules"},
    )
    return FixtureCase(
        case_id="supplier-control-v1",
        event="Fixture old vendor cancelled purchase order PO-CANCEL. Investigate its impact and prepare a recovery plan under the task policy.",
        records=records, policy=contract,
        oracle={"expected_status": "prepared", "family": "direct_cancellation"},
    )


def _reidentify(case: FixtureCase, case_id: str, *, records=None, event=None, oracle=None,
                order_ids=None) -> FixtureCase:
    policy = case.policy.payload()
    policy.update(contract_id=case_id, task_id=case_id)
    if order_ids is not None:
        policy["sales_order_ids"] = list(order_ids)
        policy["budgets"] = {str(identity): 2000 for identity in order_ids}
    return FixtureCase(case_id=case_id, event=event if event is not None else case.event,
                       records=copy.deepcopy(records if records is not None else case.records),
                       policy=contract_from_payload(policy), oracle=oracle or dict(case.oracle))


def _purchase_copy(records: dict, *, identity: int, name: str, vendor: int, quantity: int, price: int):
    """Create synthetic commitment/move identities, never simulate a write method."""
    po = copy.deepcopy(records["purchase.order"][0])
    po.update(id=identity, name=name, partner_id=vendor, order_line=[identity], picking_ids=[identity])
    line = copy.deepcopy(records["purchase.order.line"][0])
    line.update(id=identity, order_id=identity, product_qty=quantity, price_unit=price, move_ids=[identity])
    picking = copy.deepcopy(records["stock.picking"][0])
    picking.update(id=identity, move_ids=[identity])
    move = copy.deepcopy(records["stock.move"][0])
    move.update(id=identity, product_uom_qty=quantity, picking_id=identity, purchase_line_id=identity)
    for model, row in [("purchase.order", po), ("purchase.order.line", line),
                       ("stock.picking", picking), ("stock.move", move)]:
        records[model].append(row)


def _sales_copy(records: dict, *, identity: int, quantity: int, state="sale", delivered=0):
    partner = copy.deepcopy(records["res.partner"][0])
    partner.update(id=identity + 10, name=f"Fixture customer {identity}", ref=f"C{identity}")
    records["res.partner"].append(partner)
    order = copy.deepcopy(records["sale.order"][0])
    order.update(id=identity, name=f"SO-{identity}", partner_id=partner["id"], state=state)
    line = copy.deepcopy(records["sale.order.line"][0])
    line.update(id=identity, order_id=identity, product_uom_qty=quantity, qty_delivered=delivered)
    records["sale.order"].append(order)
    records["sale.order.line"].append(line)


def development_cases() -> tuple[FixtureCase, ...]:
    """Eight development cases only; no held-out records or model runs.

    ``oracle`` is a trusted-host grading specification, never a tool response.
    A missing-budget case owns a PartialPolicy and has no executable contract.
    Cases carry input facts only; expected outcomes must be demonstrated through
    actual admission/solver/change-set code, as the dataset tests do.
    """
    base = supplier_cancellation_control()
    direct = _reidentify(base, "dev-01-direct")

    records = copy.deepcopy(base.records)
    records["purchase.order.line"][0]["product_qty"] = 5
    records["stock.move"][0]["product_uom_qty"] = 5
    next(row for row in records["res.partner"] if row["id"] == 4)["name"] = "Fixture old vendor"
    _purchase_copy(records, identity=2, name="PO-OTHER", vendor=4, quantity=5, price=65)
    duplicate = _reidentify(base, "dev-02-duplicate-supplier", records=records,
                            oracle={"expected_status": "prepared", "family": "duplicate_supplier_name"})

    records = copy.deepcopy(base.records)
    records["purchase.order.line"][0]["product_qty"] = 5
    records["stock.move"][0]["product_uom_qty"] = 5
    offer = copy.deepcopy(records["product.supplierinfo"][1])
    offer.update(id=4, price=55)
    records["product.supplierinfo"].append(offer)
    _purchase_copy(records, identity=2, name="PO-OTHER-PRICE", vendor=3, quantity=5, price=55)
    multi_offer = _reidentify(base, "dev-03-offer-identity", records=records,
                             oracle={"expected_status": "prepared", "family": "multiple_supplier_offers",
                                     "required_offer_key": "V-OLD|FIN|1|60.0|1"})

    records = copy.deepcopy(base.records)
    records["sale.order.line"][0]["product_uom_qty"] = 4
    _sales_copy(records, identity=2, quantity=6)
    _sales_copy(records, identity=3, quantity=7, state="draft")
    # ScopeGuard currently rejects any preexisting delivery in the collected
    # scope, including unselected orders. Keep this prepared control inside
    # that boundary; delivered-demand filtering is a separate reader test.
    _sales_copy(records, identity=4, quantity=5, state="cancel")
    for model in ("purchase.order", "stock.picking"):
        records[model][0]["origin"] = "SO-1, SO-2"
    expanded = _reidentify(base, "dev-04-demand-scope", records=records, order_ids=[1, 2],
                           event=base.event + " The initial notice mentions SO-1; check for related active demand.",
                           oracle={"expected_status": "prepared", "family": "active_demand_with_decoys",
                                   "required_order_ids": [1, 2]})

    public = {
        "kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "dev-05-missing-budget", "task_id": "dev-05-missing-budget",
        "task_type": "supplier_cancellation", "anchor_date": "2026-01-01", "margin": 0.2,
        "margin_basis": "new_spend", "time_granularity": "day",
        "orders": {"1": {"authorized": True, "budget_cap": None}},
        "sources": {"authorization": "synthetic task authorizes order 1; its budget is undecided"},
    }
    missing_budget = FixtureCase(
        case_id="dev-05-missing-budget", event=base.event, records=copy.deepcopy(base.records),
        policy=policy_from_payload(public),
        oracle={"expected_status": "needs_clarification", "family": "undecided_business_budget",
                "required_question_fields": ["sales_order_budget_caps.1"]},
    )
    ambiguous = _reidentify(duplicate, "dev-06-ambiguous-event",
                            event="Fixture old vendor cancelled a confirmed supply commitment. Investigate and prepare a recovery plan.",
                            oracle={"expected_status": "needs_clarification", "family": "unresolvable_event_identity",
                                    "required_question_fields": ["disruption.purchase_order"]})

    records = copy.deepcopy(base.records)
    for center in records["mrp.workcenter"]:
        center["note"] = center["note"].replace("100 total processing minutes", "0 total processing minutes")
    next(row for row in records["res.partner"] if row["id"] == 4)["comment"] = (
        "Maximum order quantity for FIN: 0 units")
    infeasible = _reidentify(base, "dev-07-infeasible", records=records,
                             oracle={"expected_status": "infeasible", "family": "no_replacement_capacity",
                                     "required_reason": "planning_model_infeasible"})

    records = copy.deepcopy(base.records)
    product = copy.deepcopy(records["product.product"][1])
    product.update(id=3, name="LEAF", default_code="LEAF", product_tmpl_id=3)
    records["product.product"].append(product)
    bom = copy.deepcopy(records["mrp.bom"][0])
    bom.update(id=2, product_tmpl_id=2)
    records["mrp.bom"].append(bom)
    line = copy.deepcopy(records["mrp.bom.line"][0])
    line.update(id=2, bom_id=2, product_id=3, product_qty=1)
    records["mrp.bom.line"].append(line)
    operation = copy.deepcopy(records["mrp.routing.workcenter"][0])
    operation.update(id=2, bom_id=2)
    records["mrp.routing.workcenter"].append(operation)
    for center in records["mrp.workcenter"]:
        center["note"] += "\n" + json.dumps({"product_code": "RAW", "time_per_unit_minutes": 1,
                                              "unit_cost": 1, "lead_days": 1})
    records["product.supplierinfo"][0]["product_tmpl_id"] = 3
    next(row for row in records["res.partner"] if row["id"] == 2)["comment"] = (
        "Maximum order quantity for LEAF: 100 units")
    quant = copy.deepcopy(records["stock.quant"][1])
    quant.update(id=3, product_id=3)
    records["stock.quant"].append(quant)
    unsupported = _reidentify(base, "dev-08-multilevel-unsupported", records=records,
                              oracle={"expected_status": "unsupported", "family": "multilevel_execution",
                                      "required_boundary": "one manufactured target and one-level BoM"})
    return direct, duplicate, multi_offer, expanded, missing_budget, ambiguous, infeasible, unsupported

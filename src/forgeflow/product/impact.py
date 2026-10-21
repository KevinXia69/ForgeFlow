"""Bound public business relationships; never evidence of physical delivery."""
from __future__ import annotations

import copy
import json
import re
from collections import defaultdict
from datetime import date

from forgeflow.recovery.change_set import canonical_hash, target, validate_projection
from forgeflow.recovery.storage import digest

from .readiness import build_order_readiness, project_order_readiness

_MISSING = ["actual_receipt_unmeasured", "shipping_unmeasured", "actual_delivery_unmeasured",
            "component_order_allocation_unrecorded"]
_DOC_KEYS = {"product_code", "quantity", "product_uom_id", "vendor_ref", "offer_key", "workcenter_code",
             "supply_role", "start_day", "available_day", "origin_tokens"}
_SOURCE_KEYS = {"disruption", "orders", "operations", "stock", "components",
                "sales_order_flows", "component_flows", "unused_purchase_units"}
_ARTIFACTS = {"request", "snapshot", "blueprint", "plan", "change_set", "order_readiness"}


def _need(condition):
    if not condition:
        raise ValueError("invalid_impact_chain")


def _integer(value, *, positive=False, maximum=2**53 - 1):
    _need(type(value) is int and int(positive) <= value <= maximum)
    return value


def _name(value):
    _need(isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._:|/-]{0,127}", value)
          is not None and "://" not in value and ".." not in value)
    return value


def _shape(value, keys):
    _need(type(value) is dict and set(value) == set(keys))


def _rows(value, maximum, *, nonempty=False):
    _need(type(value) is list and int(nonempty) <= len(value) <= maximum)
    return value


def _token(value, model=None):
    _need(isinstance(value, str) and re.fullmatch(
        r"(?:existing|new):(?:sale\.order|mrp\.production|purchase\.order):[0-9]{1,16}", value) is not None)
    if model:
        _need(value.split(":")[1] == model)
    return value


def _source(artifact, record, fields):
    return {"projection": "source_facts", "artifact": artifact, "record": record, "fields": sorted(fields)}


def _clean_doc(row, kind):
    if row is None:
        return None
    return {"product_code": row["product_code"], "quantity": row["quantity"],
            "product_uom_id": row["product_uom_id"], "vendor_ref": row.get("vendor_ref"), "offer_key": row.get("offer_key"),
            "workcenter_code": row.get("workcenter_code"),
            "supply_role": row.get("supply_role", "finished"), "start_day": row.get("start_day"),
            "available_day": row["due_day" if kind == "manufacturing_order" else "arrival_day"],
            "origin_tokens": [origin["token"] for origin in row["origin_refs"]]}


def build_impact_chain(request, snapshot, blueprint, fresh_plan, changes, readiness, *,
                       plan_id, snapshot_sha256, product_constraints=None, product_objective=None):
    """Project already-bound source records after independent change-set validation.

    ``fresh_plan`` is the complete solver artifact, not only its ``plan`` member.
    The caller binds plan_id to its manifest; this module verifies artifact and
    snapshot identities and never reads files, executes a solver or accesses ERP.
    """
    try:
        _need(digest(snapshot) == snapshot_sha256)
        _need(fresh_plan["blueprint_sha256"] == canonical_hash(blueprint) and fresh_plan["odoo_writes"] is False)
        validate_projection(changes, request, snapshot, blueprint, fresh_plan,
                            product_constraints=product_constraints, product_objective=product_objective)
        expected = build_order_readiness(request, blueprint, fresh_plan["plan"], changes,
                                         plan_id=plan_id, snapshot_sha256=snapshot_sha256)
        _need(readiness == expected)
        operations = [{"entity_ref": op["target"]["token"], "model": op["target"]["model"],
                       "record_id": op["target"]["id"], "document_ref": op["target"]["name"],
                       "operation_id": op["operation_id"], "action": op["action"],
                       "before": _clean_doc(op["before"], op["kind"]),
                       "after": _clean_doc(op["after"], op["kind"])} for op in changes["operations"]]
        by_name = {op["document_ref"]: op["entity_ref"] for op in operations if op["record_id"] is not None}
        repair = blueprint["repair_context"]
        disruption = {"kind": repair["kind"], "vendor_ref": repair.get("broken_supplier_vendor_ref"),
                      "offer_key": repair.get("broken_supplier_offer_key"),
                      "workcenter_code": repair.get("broken_workcenter_code"),
                      "broken_tokens": sorted(by_name[name] for name in
                          repair["broken_purchase_order_refs"] + repair["broken_manufacturing_order_refs"])}
        sales = {row["name"]: row for row in snapshot["models"]["sale.order"]
                 if row["id"] in request["sales_order_ids"]}
        orders = [{"entity_ref": target("sale.order", sales[row["order_ref"]])["token"],
                   "order_ref": row["order_ref"], "product_code": request["target_product_code"],
                   "quantity": row["demand_units"], "customer_due_day": row["customer_due_day"],
                   "customer_due_date": row["customer_due_date"]} for row in readiness["orders"]]
        projected = changes["projected_state"]
        source_facts = {"disruption": disruption, "orders": orders, "operations": operations,
            "stock": [{"entity_ref": f"stock:{sku}", "product_code": sku, "quantity": quantity,
                       "used_units": projected["stock_used"][sku],
                       "remaining_units": projected["stock_remaining_unallocated"][sku]}
                      for sku, quantity in sorted(blueprint["product_stock_units"].items())],
            "components": sorted([{"product_code": row["product"]["code"], "per_unit": row["per_unit"]}
                                  for row in blueprint["manufactured_products"][0]["components"]],
                                 key=lambda row: row["product_code"]),
            "sales_order_flows": [{"source_token": row["source_token"],
                "order_token": row["sales_order"]["token"], "order_ref": row["sales_order"]["name"],
                **{key: row[key] for key in ("product_code", "quantity", "available_day", "plan_ref")}}
                for row in projected["sales_order_flows"]],
            "component_flows": [{key: row[key] for key in ("source_token", "manufacturing_token",
                "quantity", "product_code", "available_day", "required_day", "source_plan_ref")}
                for row in projected["component_flows"]],
            "unused_purchase_units": [{"source_token": token,
                "product_code": next(op["after"]["product_code"] for op in operations if op["entity_ref"] == token),
                "quantity": quantity} for token, quantity in sorted(projected["unused_purchase_units"].items())]}
        basis = {"plan_id": plan_id, "snapshot_sha256": snapshot_sha256,
            "anchor_date": request["anchor_date"], "target_product_code": request["target_product_code"],
            "quantity_basis": "product_units", "time_basis": "planned_supply_availability_days",
            "source_sha256": {key: digest(value) for key, value in (
                ("request", request), ("snapshot", snapshot), ("blueprint", blueprint),
                ("plan", fresh_plan), ("change_set", changes), ("order_readiness", readiness))}}
        result = _derive(basis, source_facts, readiness)
        return project_impact_chain(result, plan_id=plan_id, snapshot_sha256=snapshot_sha256, readiness=readiness)
    except (KeyError, TypeError, ValueError, OverflowError, StopIteration):
        raise ValueError("invalid_impact_chain") from None


def _validate_sources(basis, source, readiness):
    _shape(source, _SOURCE_KEYS)
    target_code = _name(basis["target_product_code"])
    orders, operations, stock, components = {}, {}, {}, {}
    ready = {row["order_ref"]: row for row in readiness["orders"]}
    for row in _rows(source["orders"], 128, nonempty=True):
        _shape(row, ("entity_ref", "order_ref", "product_code", "quantity", "customer_due_day", "customer_due_date"))
        token = _token(row["entity_ref"], "sale.order")
        _need(token.startswith("existing:") and token not in orders and row["order_ref"] in ready)
        _name(row["order_ref"])
        _integer(row["quantity"], positive=True)
        _integer(row["customer_due_day"], maximum=36500)
        expected = ready[row["order_ref"]]
        _need(row["product_code"] == target_code and row["quantity"] == expected["demand_units"]
              and row["customer_due_day"] == expected["customer_due_day"]
              and row["customer_due_date"] == expected["customer_due_date"])
        orders[token] = row
    _need(len(orders) == len(ready) and {row["order_ref"] for row in orders.values()} == set(ready))
    for row in _rows(source["components"], 128, nonempty=True):
        _shape(row, ("product_code", "per_unit"))
        sku = _name(row["product_code"])
        _need(sku != target_code and sku not in components)
        components[sku] = _integer(row["per_unit"], positive=True)
    for row in _rows(source["operations"], 256, nonempty=True):
        _shape(row, ("entity_ref", "model", "record_id", "document_ref", "operation_id", "action", "before", "after"))
        token = _token(row["entity_ref"], row["model"])
        _need(row["model"] in {"purchase.order", "mrp.production"} and token not in operations
              and row["operation_id"] == f"op:{token}")
        if token.startswith("existing:"):
            _integer(row["record_id"], positive=True)
            _name(row["document_ref"])
            _need(token == f"existing:{row['model']}:{row['record_id']}" and row["before"] is not None)
        else:
            _need(row["record_id"] is None and row["document_ref"] is None and row["before"] is None)
        _need(row["before"] is not None or row["after"] is not None)
        action = "create" if row["before"] is None else "cancel" if row["after"] is None else None
        _need(row["action"] == action if action else row["action"] in {"keep", "update"})
        _need(row["action"] != "keep" or row["before"] == row["after"])
        for phase in ("before", "after"):
            doc = row[phase]
            if doc is None:
                continue
            _shape(doc, _DOC_KEYS)
            sku = _name(doc["product_code"])
            _integer(doc["quantity"], positive=True)
            _integer(doc["product_uom_id"], positive=True)
            _need(type(doc["available_day"]) is int and (-36500 if phase == "before" else 0) <= doc["available_day"] <= 36500)
            _rows(doc["origin_tokens"], 128)
            _need(len(doc["origin_tokens"]) == len(set(doc["origin_tokens"])))
            for origin in doc["origin_tokens"]:
                _token(origin)
            if row["model"] == "mrp.production":
                _need(sku == target_code and doc["vendor_ref"] is None and doc["offer_key"] is None and doc["supply_role"] == "finished")
                _name(doc["workcenter_code"])
                _need(type(doc["start_day"]) is int and (-36500 if phase == "before" else 0) <= doc["start_day"] <= 36500)
                _need(doc["start_day"] <= doc["available_day"])
            else:
                _name(doc["vendor_ref"])
                _name(doc["offer_key"])
                _need(doc["workcenter_code"] is None and doc["start_day"] is None
                      and doc["supply_role"] == ("finished" if sku == target_code else "component")
                      and (sku == target_code or sku in components))
        operations[token] = row
    for row in _rows(source["stock"], 129, nonempty=True):
        _shape(row, ("entity_ref", "product_code", "quantity", "used_units", "remaining_units"))
        sku = _name(row["product_code"])
        _need(row["entity_ref"] == f"stock:{sku}" and row["entity_ref"] not in stock)
        _need(_integer(row["quantity"]) == _integer(row["used_units"]) + _integer(row["remaining_units"]))
        stock[row["entity_ref"]] = row
    _need({row["product_code"] for row in stock.values()} == set(components) | {target_code})
    disruption = source["disruption"]
    _shape(disruption, ("kind", "vendor_ref", "offer_key", "workcenter_code", "broken_tokens"))
    broken = _rows(disruption["broken_tokens"], 256, nonempty=True)
    _need(len(broken) == len(set(broken)))
    if disruption["kind"] == "supplier_cancellation":
        _name(disruption["vendor_ref"])
        _name(disruption["offer_key"])
        _need(disruption["workcenter_code"] is None)
    else:
        _need(disruption["kind"] == "workcenter_outage" and disruption["vendor_ref"] is None
              and disruption["offer_key"] is None)
        _name(disruption["workcenter_code"])
    for token in broken:
        _need(token in operations and operations[token]["before"] is not None)
        op = operations[token]
        if disruption["kind"] == "supplier_cancellation":
            _need(op["model"] == "purchase.order" and op["before"]["vendor_ref"] == disruption["vendor_ref"]
                  and op["before"]["offer_key"] == disruption["offer_key"])
        else:
            _need(op["model"] == "mrp.production" and op["before"]["workcenter_code"] == disruption["workcenter_code"])
    def broken_doc(op, doc):
        if doc is None:
            return False
        if disruption["kind"] == "supplier_cancellation":
            return (op["model"] == "purchase.order" and doc["vendor_ref"] == disruption["vendor_ref"]
                    and doc["offer_key"] == disruption["offer_key"])
        return op["model"] == "mrp.production" and doc["workcenter_code"] == disruption["workcenter_code"]
    _need(set(broken) == {token for token, op in operations.items() if broken_doc(op, op["before"])})
    _need(not any(broken_doc(op, op["after"]) for op in operations.values()))
    for token, op in operations.items():
        for phase in ("before", "after"):
            doc = op[phase]
            if doc is None:
                continue
            for origin in doc["origin_tokens"]:
                if phase == "before":
                    _need(origin in orders or (origin in operations and operations[origin]["model"] == "mrp.production"
                          and operations[origin]["before"] is not None))
                elif op["model"] == "purchase.order" and doc["supply_role"] == "component":
                    _need(origin in operations and operations[origin]["model"] == "mrp.production"
                          and operations[origin][phase] is not None and origin != token)
                else:
                    _need(origin in orders)
    # Baseline origin is only a relationship scope, including direct component
    # PO -> SO references. Preserve it without manufacturing an allocation, but
    # refuse circular MO origins rather than drawing a causal-looking cycle.
    visiting, visited = set(), set()
    def visit(token):
        _need(token not in visiting)
        if token in visited or token in orders:
            return
        visiting.add(token)
        for origin in operations[token]["before"]["origin_tokens"]:
            visit(origin)
        visiting.remove(token)
        visited.add(token)
    for token, op in operations.items():
        if op["before"] is not None:
            visit(token)
    _validate_flows(source, orders, operations, stock, components, ready, target_code)
    return orders, operations, stock


def _validate_flows(source, orders, operations, stock, components, ready, target_code):
    used, component_used, order_flows, origins = defaultdict(int), defaultdict(int), defaultdict(list), defaultdict(set)
    active = {token: row["after"] for token, row in operations.items() if row["after"] is not None}

    def supply(flow):
        token, sku = flow["source_token"], flow["product_code"]
        _integer(flow["quantity"], positive=True)
        _integer(flow["available_day"], maximum=36500)
        if token in stock:
            _need(stock[token]["product_code"] == sku and flow["available_day"] == 0)
        else:
            _need(token in active and active[token]["product_code"] == sku
                  and flow["available_day"] == active[token]["available_day"])
        used[token] += flow["quantity"]

    seen = set()
    for flow in _rows(source["sales_order_flows"], 4096):
        _shape(flow, ("source_token", "order_token", "order_ref", "product_code", "quantity", "available_day", "plan_ref"))
        signature = (flow["source_token"], flow["order_token"], flow["plan_ref"])
        _need(signature not in seen)
        seen.add(signature)
        supply(flow)
        token = flow["order_token"]
        _need(token in orders and flow["order_ref"] == orders[token]["order_ref"] and flow["product_code"] == target_code)
        if flow["source_token"] in stock:
            _need(flow["plan_ref"] is None)
            category = "stock_units"
        else:
            _name(flow["plan_ref"])
            category = "manufactured_units" if operations[flow["source_token"]]["model"] == "mrp.production" else "purchased_units"
            origins[flow["source_token"]].add(token)
        order_flows[flow["order_ref"]].append((category, flow["quantity"], flow["available_day"]))
    seen = set()
    for flow in _rows(source["component_flows"], 4096):
        _shape(flow, ("source_token", "manufacturing_token", "product_code", "quantity", "available_day", "required_day", "source_plan_ref"))
        signature = (flow["source_token"], flow["manufacturing_token"], flow["source_plan_ref"])
        _need(signature not in seen)
        seen.add(signature)
        supply(flow)
        token, sku = flow["manufacturing_token"], flow["product_code"]
        _integer(flow["required_day"], maximum=36500)
        _need(sku in components and token in active and operations[token]["model"] == "mrp.production"
              and flow["required_day"] == active[token]["start_day"]
              and flow["available_day"] <= flow["required_day"])
        if flow["source_token"] in stock:
            _need(flow["source_plan_ref"] is None)
        else:
            _name(flow["source_plan_ref"])
            _need(operations[flow["source_token"]]["model"] == "purchase.order")
            origins[flow["source_token"]].add(token)
        component_used[token, sku] += flow["quantity"]
    unused = {}
    for row in _rows(source["unused_purchase_units"], 256):
        _shape(row, ("source_token", "product_code", "quantity"))
        token = row["source_token"]
        _need(token in active and token not in unused and operations[token]["model"] == "purchase.order"
              and active[token]["supply_role"] == "component" and row["product_code"] == active[token]["product_code"])
        unused[token] = _integer(row["quantity"], positive=True)
    for token, row in stock.items():
        _need(used[token] == row["used_units"])
    for token, doc in active.items():
        _need(used[token] + unused.get(token, 0) == doc["quantity"] and origins[token] == set(doc["origin_tokens"]))
        if operations[token]["model"] == "mrp.production":
            for sku, per_unit in components.items():
                _need(component_used[token, sku] == doc["quantity"] * per_unit)
    for name, expected in ready.items():
        flows = order_flows[name]
        _need(sum(row[1] for row in flows) == expected["covered_units"]
              and max((row[2] for row in flows), default=None) == expected["planned_ready_day"])
        for category in ("stock_units", "manufactured_units", "purchased_units"):
            _need(sum(row[1] for row in flows if row[0] == category) == expected[category])


def _derive(basis, source, readiness):
    orders, operations, stock = _validate_sources(basis, source, readiness)
    nodes, edges = [], []

    def node(key, kind, phase, label, sku, quantity, action, facts, refs):
        nodes.append({"key": key, "kind": kind, "phase": phase, "label": label,
                      "product_code": sku, "quantity": quantity, "unit": "units" if sku else None,
                      "action": action, "facts": copy.deepcopy(facts), "source_refs": refs})

    def edge(key, start, end, phase, relation, sku, quantity, refs, available=None, required=None):
        edges.append({"key": key, "from": start, "to": end, "phase": phase, "relation": relation,
                      "product_code": sku, "quantity": quantity,
                      "quantity_basis": "recorded_scope_quantity_unknown" if phase == "baseline" else
                          "unallocated_supply" if relation == "unused_supply" else "exact_plan_allocation",
                      "available_day": available, "required_day": required, "source_refs": refs})

    disruption = source["disruption"]
    label = disruption["vendor_ref"] if disruption["kind"] == "supplier_cancellation" else disruption["workcenter_code"]
    node("disruption", "disruption", "baseline", label, None, None, None, disruption,
         [_source("request", "disruption", ("kind", "vendor_ref", "workcenter_code")),
          _source("blueprint", "disruption", ("offer_key", "broken_tokens"))])
    for i, row in enumerate(source["orders"]):
        node(f"order:{row['entity_ref']}", "sales_order", "shared", row["order_ref"], row["product_code"],
             row["quantity"], None, row, [_source("snapshot", f"orders.{i}", ("entity_ref", "order_ref", "product_code", "quantity")),
             _source("order_readiness", f"orders.{i}", ("customer_due_day", "customer_due_date"))])
    for i, op in enumerate(source["operations"]):
        token = op["entity_ref"]
        kind = "manufacturing_order" if op["model"] == "mrp.production" else "purchase_order"
        for phase, field in (("baseline", "before"), ("planned", "after")):
            doc = op[field]
            if doc is None:
                continue
            name = op["document_ref"] or f"Planned {'manufacturing' if kind == 'manufacturing_order' else 'purchase'} {token.rsplit(':', 1)[1]}"
            facts = {**doc, **{key: op[key] for key in ("entity_ref", "model", "record_id", "document_ref", "operation_id")}}
            node(f"{phase}:{token}", kind, phase, name, doc["product_code"], doc["quantity"], op["action"], facts,
                 [_source("change_set", f"operations.{i}", ("entity_ref", "model", "record_id", "document_ref", "operation_id", "action")),
                  _source("change_set", f"operations.{i}.{field}", doc)])
        if op["before"] is not None:
            for j, origin in enumerate(op["before"]["origin_tokens"]):
                end = f"order:{origin}" if origin in orders else f"baseline:{origin}"
                edge(f"origin:{i}:{j}", f"baseline:{token}", end, "baseline", "origin_scope",
                     op["before"]["product_code"], None,
                     [_source("change_set", f"operations.{i}.before", ("origin_tokens", "product_code"))])
    for i, token in enumerate(disruption["broken_tokens"]):
        edge(f"disruption:{i}", "disruption", f"baseline:{token}", "baseline", "disruption_scope",
             operations[token]["before"]["product_code"], None, [_source("blueprint", "disruption", ("kind", "broken_tokens"))])
    for i, row in enumerate(source["stock"]):
        node(f"planned:{row['entity_ref']}", "stock", "planned", f"Stock {row['product_code']}", row["product_code"],
             row["quantity"], None, row, [_source("blueprint", f"stock.{i}", ("entity_ref", "product_code", "quantity")),
             _source("change_set", f"stock.{i}", ("used_units", "remaining_units"))])
    for i, row in enumerate(source["sales_order_flows"]):
        edge(f"sales:{i}", f"planned:{row['source_token']}", f"order:{row['order_token']}", "planned", "sales_allocation",
             row["product_code"], row["quantity"], [_source("change_set", f"sales_order_flows.{i}", row)], row["available_day"])
    for i, row in enumerate(source["component_flows"]):
        edge(f"component:{i}", f"planned:{row['source_token']}", f"planned:{row['manufacturing_token']}", "planned", "component_allocation",
             row["product_code"], row["quantity"], [_source("change_set", f"component_flows.{i}", row)], row["available_day"], row["required_day"])
    for i, row in enumerate(source["unused_purchase_units"]):
        key = f"unallocated:{row['source_token']}"
        refs = [_source("change_set", f"unused_purchase_units.{i}", row)]
        node(key, "unallocated_supply", "planned", f"Unallocated {row['product_code']}", row["product_code"], row["quantity"], None, row, refs)
        edge(f"unused:{i}", f"planned:{row['source_token']}", key, "planned", "unused_supply", row["product_code"], row["quantity"], refs)
    node_keys = {row["key"] for row in nodes}
    _need(len(node_keys) == len(nodes) and len(nodes) <= 1024 and len(edges) <= 8192)
    _need(all(row["from"] in node_keys and row["to"] in node_keys for row in edges))

    def reachable(start, phase, reverse=False):
        adjacency = defaultdict(list)
        for row in edges:
            if row["phase"] == phase:
                adjacency[row["to"] if reverse else row["from"]].append(row["from"] if reverse else row["to"])
        seen, pending = set(), [start]
        while pending:
            current = pending.pop()
            if current not in seen:
                seen.add(current)
                pending.extend(adjacency[current])
        return seen

    impacted = reachable("disruption", "baseline")
    summaries = []
    for row in readiness["orders"]:
        order = next(item for item in orders.values() if item["order_ref"] == row["order_ref"])
        key = f"order:{order['entity_ref']}"
        baseline = reachable(key, "baseline", True) & impacted
        planned = reachable(key, "planned", True)
        missing = ["baseline_allocation_quantity_unrecorded"] if key in baseline else ["baseline_relationship_unrecorded"]
        summaries.append({"order_ref": row["order_ref"], "node_key": key,
            "baseline_node_keys": sorted(baseline), "planned_node_keys": sorted(planned),
            "baseline_edge_keys": [edge["key"] for edge in edges if edge["phase"] == "baseline" and edge["from"] in baseline and edge["to"] in baseline],
            "planned_edge_keys": [edge["key"] for edge in edges if edge["phase"] == "planned" and edge["from"] in planned and edge["to"] in planned],
            "readiness": copy.deepcopy(row), "missing": missing + _MISSING})
    return {"schema_version": 1, "basis": copy.deepcopy(basis), "source_facts": copy.deepcopy(source),
            "nodes": nodes, "edges": edges, "orders": summaries, "missing": list(_MISSING)}


def project_impact_chain(value, *, plan_id, snapshot_sha256, readiness):
    """Reject altered public facts/edges and preserve absent legacy data as None."""
    if value is None:
        return None
    try:
        _shape(value, ("schema_version", "basis", "source_facts", "nodes", "edges", "orders", "missing"))
        _need(type(value["schema_version"]) is int and value["schema_version"] == 1)
        _need(len(json.dumps(value, ensure_ascii=False, allow_nan=False)) <= 1_000_000)
        for key in (plan_id, snapshot_sha256):
            _need(isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key) is not None)
        basis = value["basis"]
        _shape(basis, ("plan_id", "snapshot_sha256", "anchor_date", "target_product_code", "quantity_basis", "time_basis", "source_sha256"))
        _need(basis["plan_id"] == plan_id and basis["snapshot_sha256"] == snapshot_sha256
              and basis["quantity_basis"] == "product_units" and basis["time_basis"] == "planned_supply_availability_days")
        _need(date.fromisoformat(basis["anchor_date"]).isoformat() == basis["anchor_date"])
        _shape(basis["source_sha256"], _ARTIFACTS)
        _need(all(isinstance(key, str) and re.fullmatch(r"[a-f0-9]{64}", key) is not None
                  for key in basis["source_sha256"].values()))
        _need(basis["source_sha256"]["snapshot"] == snapshot_sha256)
        checked = project_order_readiness(readiness, plan_id=plan_id, snapshot_sha256=snapshot_sha256)
        _need(checked is not None and checked["basis"]["anchor_date"] == basis["anchor_date"]
              and digest(checked) == basis["source_sha256"]["order_readiness"])
        expected = _derive(basis, value["source_facts"], checked)
        # JSON comparison also rejects bool/int substitutions and duplicate-free
        # source projections deterministically define every node, edge and ref.
        _need(json.dumps(value, sort_keys=True, allow_nan=False) == json.dumps(expected, sort_keys=True, allow_nan=False))
    except (KeyError, TypeError, ValueError, OverflowError, RecursionError):
        raise ValueError("invalid_impact_chain") from None
    return copy.deepcopy(value)

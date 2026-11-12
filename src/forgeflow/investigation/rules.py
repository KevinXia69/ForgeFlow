"""Deterministic investigation baseline using only an event and public tools.

This module never receives a case, raw records, policy object, or oracle. It
recognizes a small event grammar, follows business relationships, and uses the
same evidence admission and preparation tools as an agent. Unrecognized inputs
are recorded failures, not silently replaced by known development answers.
"""

from __future__ import annotations

import re
from collections.abc import Callable

TERMINAL = frozenset({"prepared", "needs_clarification", "unsupported", "infeasible",
                      "budget_exhausted", "technical_failure"})


class _Done(Exception):
    def __init__(self, result):
        self.result = result


def _mentioned(text, value):
    return isinstance(value, str) and bool(value) and bool(re.search(
        r"(?<![\w-])" + re.escape(value) + r"(?![\w-])", text, re.IGNORECASE))


class _Rules:
    def __init__(self, event_text, dispatch):
        self.event, self.dispatch = event_text, dispatch
        self.trace = []
        self.records = {}

    def call(self, action, payload):
        try:
            result = self.dispatch(action, payload)
        except Exception as error:
            self.trace.append({"action": action, "payload": payload,
                               "error_type": type(error).__name__})
            raise
        self.trace.append({"action": action, "payload": payload, "result": result})
        if not isinstance(result, dict):
            raise ValueError("invalid_rule_tool_response")
        for record in result.get("records", []):
            self.records[(record["entity"], record["id"])] = record
        if result.get("status") in TERMINAL:
            raise _Done(result)
        return result

    def finish(self, status, reason, question=""):
        self.call("finish", {"status": status, "reason": reason, "question": question})
        raise ValueError("finish_did_not_return_terminal_state")

    def query(self, kind, arguments):
        return self.call("query", {"kind": kind, "arguments": arguments})

    def record(self, entity, identity):
        key = (entity, str(identity))
        if key not in self.records:
            rows = self.query("record", {"entity": entity, "id": str(identity)})["records"]
            if len(rows) != 1:
                self.finish("technical_failure", "rule_business_record_missing")
        return self.records[key]

    def pages(self, kind, arguments):
        rows, cursor, seen = [], None, set()
        for _ in range(1001):
            result = self.query(kind, {**arguments, "limit": 50, "cursor": cursor})
            rows.extend(result["records"])
            if len(rows) > 1000:
                break
            if result["complete"]:
                return rows
            cursor = result.get("next_cursor")
            if not result["records"] or not isinstance(cursor, str) or cursor in seen:
                break
            seen.add(cursor)
        self.finish("technical_failure", "rule_query_incomplete")

    def related(self, entity, identity):
        return self.pages("relations", {"entity": entity, "id": identity})

    def investigate(self):
        if not isinstance(self.event, str) or not self.event.strip():
            self.finish("technical_failure", "rule_event_pattern_unrecognized")
        match = re.match(r"\s*(.+?)\s+(?:has\s+)?cancel(?:led|ed|s)\b", self.event, re.I)
        if match is None:
            self.finish("technical_failure", "rule_event_pattern_unrecognized")
        supplier_text = match[1].strip(" \"'")
        if not 1 <= len(supplier_text) <= 128:
            self.finish("technical_failure", "rule_event_pattern_unrecognized")
        suppliers = self.pages("entities", {"entity": "supplier", "query": supplier_text})
        suppliers = [row for row in suppliers if any(
            str(row["fields"].get(field, "")).casefold() == supplier_text.casefold()
            for field in ("name", "ref"))]
        if not suppliers:
            self.finish("needs_clarification", "supplier_identity_unresolved",
                        f"What is the exact supplier name or reference for {supplier_text}?")
        supplier_by_id = {row["id"]: row for row in suppliers}
        tokens = set(re.findall(r"(?<![\w-])(?:PO[-_][A-Za-z0-9_.-]+|PO\d+|P\d+)(?![\w-])",
                                self.event, re.I))
        tokens = {token.rstrip(".,;:") for token in tokens}
        purchases = {}
        if tokens:
            for token in sorted(tokens):
                rows = self.pages("entities", {"entity": "purchase_order", "query": token})
                rows = [row for row in rows if row["fields"]["name"].casefold() == token.casefold()]
                if not rows:
                    self.finish("needs_clarification", "purchase_identity_unresolved",
                                f"What is the correct purchase order number for {token}?")
                purchases.update({row["id"]: row for row in rows})
        else:
            for supplier in suppliers:
                purchases.update({row["id"]: row for row in self.related("supplier", supplier["id"])
                                  if row["entity"] == "purchase_order"})
        pairs = []
        for purchase in purchases.values():
            if (purchase["fields"]["supplier_id"] not in supplier_by_id
                    or purchase["fields"]["state"] not in {"purchase", "done"}):
                continue
            for line in self.related("purchase_order", purchase["id"]):
                if (line["entity"] != "purchase_order_line"
                        or line["fields"]["quantity"] <= line["fields"]["received_quantity"]):
                    continue
                product = self.record("product", line["fields"]["product_id"])
                pairs.append((supplier_by_id[purchase["fields"]["supplier_id"]], purchase, line, product))
        named_products = [pair for pair in pairs if any(
            _mentioned(self.event, pair[3]["fields"][field]) for field in ("name", "default_code"))]
        if named_products:
            pairs = named_products
        if len(pairs) != 1:
            names = sorted({purchase["fields"]["name"] for _, purchase, _, _ in pairs})
            question = ("Does the cancellation concern " + " or ".join(names) + "?" if len(names) >= 2
                        else f"Which purchase order did {supplier_text} cancel?")
            self.finish("needs_clarification", "event_supply_ambiguous", question)
        supplier, purchase, line, product = pairs[0]
        relations = self.related("product", product["id"])
        supplied, offer_relations = product, relations

        def active_orders(rows):
            orders = {}
            for row in rows:
                if (row["entity"] != "sales_order_line"
                        or row["fields"]["quantity"] <= row["fields"]["delivered_quantity"]):
                    continue
                order = self.record("sales_order", row["fields"]["order_id"])
                if order["fields"]["state"] in {"sale", "done"}:
                    orders[int(order["id"])] = order
            return orders

        orders = active_orders(relations)
        if not orders:
            # One public BoM step can prove the affected finished product for
            # a canceled component. Never select a hidden contract target or
            # guess between multiple parents with active customer demand.
            parents = {}
            for use in relations:
                if use["entity"] != "bom_line":
                    continue
                bom = self.record("bom", use["fields"]["bom_id"])
                if not bom["fields"]["active"] or bom["fields"]["type"] != "normal":
                    continue
                for parent in self.related("bom", bom["id"]):
                    if parent["entity"] != "product" or parent["id"] == supplied["id"]:
                        continue
                    demand = active_orders(self.related("product", parent["id"]))
                    if demand:
                        parents[parent["id"]] = (parent, demand)
            if len(parents) > 1:
                self.finish("needs_clarification", "component_target_ambiguous",
                            "Which affected finished product is authorized for this recovery: "
                            + " or ".join(sorted(row[0]["fields"]["default_code"] for row in parents.values())) + "?")
            if parents:
                product, orders = next(iter(parents.values()))
        if not orders:
            self.finish("technical_failure", "rule_no_active_demand")
        policy = self.query("policy", {})["records"][0]
        request = {
            "target_product_code": product["fields"]["default_code"],
            "sales_order_ids": sorted(orders), "anchor_date": policy["fields"]["anchor_date"],
            "margin": policy["fields"]["margin"], "sales_order_budget_caps": {},
            "disruption": {"kind": policy["fields"]["task_type"], "vendor_ref": supplier["fields"]["ref"]},
        }
        references = []

        def bind(field, record, source_field):
            references.append({"field": field, "observation_id": record["observation_id"],
                               "source_field": source_field})

        bind("target_product_code", product, "default_code")
        bind("anchor_date", policy, "anchor_date")
        bind("margin", policy, "margin")
        bind("disruption.kind", policy, "task_type")
        bind("disruption.vendor_ref", supplier, "ref")
        offers = [row for row in offer_relations if row["entity"] == "supplier_offer"
                  and row["fields"]["supplier_id"] == supplier["id"]
                  and row["fields"]["product_id"] == supplied["id"]
                  and abs(row["fields"]["unit_price"] - line["fields"]["unit_price"]) < 0.005]
        if len(offers) != 1 or offers[0]["fields"].get("offer_key") is None:
            self.finish("needs_clarification", "supplier_offer_unresolved",
                        f"Which supplier offer and price apply to purchase order {purchase['fields']['name']}?")
        request["disruption"]["offer_key"] = offers[0]["fields"]["offer_key"]
        bind("disruption.offer_key", offers[0], "offer_key")
        for index, identity in enumerate(sorted(orders)):
            order = orders[identity]
            authorized = self.query("policy", {"order_id": identity})["records"][0]
            if not authorized["fields"]["authorized"]:
                self.finish("needs_clarification", "order_not_authorized",
                            f"Is sales order {order['fields']['name']} authorized for this recovery, and what budget applies?")
            budget = authorized["fields"]["budget_cap"]
            if budget is None:
                self.finish("needs_clarification", "budget_not_authorized",
                            f"What budget cap is authorized for sales order {order['fields']['name']}?")
            request["sales_order_budget_caps"][str(identity)] = budget
            bind(f"sales_order_ids.{index}", order, "id")
            bind(f"sales_order_budget_caps.{identity}", authorized, "budget_cap")
        draft = {"request": request, "evidence": references,
                 "supply_observation_id": purchase["observation_id"],
                 "reason": "Fixed rule workflow: event identity, complete active demand, and explicit policy."}
        checked = self.call("validate", {"draft": draft})
        if not checked.get("valid"):
            self.finish("technical_failure", "rule_validation_failed")
        self.call("prepare", {"draft_id": checked["draft_id"]})
        self.finish("technical_failure", "rule_preparation_incomplete")


def run_rules(event_text: str, dispatch: Callable[[str, dict], dict]) -> dict:
    """Run the fixed baseline; all facts and limits come through dispatch."""
    rules = _Rules(event_text, dispatch)
    try:
        rules.investigate()
    except _Done as completed:
        result = completed.result
    except Exception:
        try:
            rules.finish("technical_failure", "rule_tool_or_schema_failure")
        except _Done as completed:
            result = completed.result
        except Exception:
            result = {"status": "technical_failure", "terminal": {"reason": "rule_dispatch_failure"},
                      "prepared": None}
    return {"method": "rules-v2", "result": result, "trace": rules.trace}

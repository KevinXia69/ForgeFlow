"""Host-only admission for a missing-budget ticket; never a prepare authority.

The task template is supplied by the trusted host, not by the model or a reply.
Identity and demand are independently proved through public business queries.
No temporary budget or executable contract is invented to make a draft pass.
"""
from __future__ import annotations

import copy
from datetime import datetime

from forgeflow.contract import TaskContract, contract_from_payload
from forgeflow.investigation.event import _entities, bind_event, trusted_event
from forgeflow.investigation.evidence import (
    component_target_unambiguous,
    related,
    supply_products,
)
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.recovery.storage import digest

TEMPLATE_KIND = "forgeflow.investigation_template"


class IntakeError(ValueError):
    pass


def require(condition, reason):
    if not condition:
        raise IntakeError(reason)


def task_template(contract: TaskContract) -> dict:
    """Retain all non-budget authorization, with no reference budget amounts."""
    fields = contract.payload()
    del fields["budgets"]
    return {"kind": TEMPLATE_KIND, "schema_version": 1, "fields": fields}


def template_fields(template: dict, policy: PartialPolicy, now: float) -> dict:
    require(isinstance(policy, PartialPolicy), "partial_policy_required")
    require(isinstance(template, dict) and set(template) == {"kind", "schema_version", "fields"}
            and template["kind"] == TEMPLATE_KIND and template["schema_version"] == 1,
            "invalid_task_template")
    fields = copy.deepcopy(template["fields"])
    required = {"schema_version", "kind", "contract_id", "task_id", "task_type", "product_code",
                "anchor_date", "margin", "margin_basis", "time_granularity", "sales_order_ids",
                "disruption", "forbidden_workcenters", "forbidden_operation_kinds",
                "original_commitment_policy", "sources", "expires_at_utc"}
    require(isinstance(fields, dict) and set(fields) == required, "invalid_task_template")
    visible = policy.payload()
    require(all(fields[key] == visible[key] for key in
                ("task_id", "task_type", "anchor_date", "margin", "margin_basis", "time_granularity")),
            "template_policy_mismatch")
    require(fields["task_type"] == "supplier_cancellation", "unsupported_template")
    ids = fields["sales_order_ids"]
    require(isinstance(ids, list) and ids and all(type(i) is int and i > 0 for i in ids)
            and len(set(ids)) == len(ids), "invalid_template_scope")
    require(set(map(str, ids)) == set(visible["orders"])
            and all(row["authorized"] for row in visible["orders"].values()),
            "template_authorization_mismatch")
    expiry = fields["expires_at_utc"]
    if expiry is not None:
        try:
            parsed = datetime.fromisoformat(expiry)
            require(parsed.tzinfo is not None and parsed.timestamp() > now, "policy_expired")
        except (TypeError, ValueError) as error:
            raise IntakeError("policy_expired") from error
    return fields


def inspect_intake(source, *, template: dict, policy: PartialPolicy, event: dict, now: float) -> dict:
    """Prove fixed identities, complete demand and exactly one missing cap."""
    fields = template_fields(template, policy, now)
    event = trusted_event(event)
    facts = {}

    def remember(row, selected=None):
        # Keep complete returned records: a ticket is invalidated by material
        # changes before confirmation. New episode observations are still fresh.
        facts[row["entity"] + ":" + row["id"]] = copy.deepcopy(row)

    def unique(entity, query, key):
        found = [r for r in _entities(source, entity, query) if r["fields"].get(key) == query]
        require(len(found) == 1, "intake_identity_ambiguous")
        remember(found[0])
        return found[0]

    product = unique("product", fields["product_code"], "default_code")
    supplier = unique("supplier", fields["disruption"]["vendor_ref"], "ref")
    allowed = supply_products(source, product, remember)
    supplies = {}
    for po in related(source, "supplier", supplier["id"]):
        if po["entity"] != "purchase_order" or po["fields"]["state"] not in {"purchase", "done"}:
            continue
        for line in related(source, "purchase_order", po["id"]):
            if (line["entity"] != "purchase_order_line"
                    or line["fields"]["product_id"] not in allowed
                    or line["fields"]["quantity"] <= line["fields"]["received_quantity"]):
                continue
            supplied = allowed[line["fields"]["product_id"]]
            if bind_event(event, source, supplier, po, supplied, remember) is None:
                supplies[po["id"], supplied["id"]] = (po, supplied)
    require(len(supplies) == 1, "intake_event_unproven")
    supply, supplied = next(iter(supplies.values()))
    remember(supply)
    if supplied["id"] != product["id"]:
        require(component_target_unambiguous(source, supplied, product, remember),
                "component_target_ambiguous")
    lines = [r for r in related(source, "purchase_order", supply["id"])
             if r["entity"] == "purchase_order_line" and r["fields"]["product_id"] == supplied["id"]
             and r["fields"]["quantity"] > r["fields"]["received_quantity"]]
    prices = {r["fields"]["unit_price"] for r in lines}
    offers = [r for r in related(source, "product", supplied["id"])
              if r["entity"] == "supplier_offer" and r["fields"]["supplier_id"] == supplier["id"]
              and r["fields"]["product_id"] == supplied["id"] and len(prices) == 1
              and abs(r["fields"]["unit_price"] - next(iter(prices))) < 0.005]
    require(len(offers) == 1, "intake_offer_ambiguous")
    require(not fields["disruption"].get("offer_key")
            or fields["disruption"]["offer_key"] == offers[0]["fields"]["offer_key"],
            "intake_offer_mismatch")
    for row in [*lines, offers[0]]:
        remember(row)
    active = {}
    for line in related(source, "product", product["id"]):
        if (line["entity"] != "sales_order_line"
                or line["fields"]["quantity"] <= line["fields"]["delivered_quantity"]):
            continue
        rows = source.query("record", {"entity": "sales_order", "id": line["fields"]["order_id"]})["records"]
        require(len(rows) == 1, "intake_order_missing")
        if rows[0]["fields"]["state"] in {"sale", "done"}:
            require(rows[0]["fields"]["state"] == "sale", "intake_order_not_active")
            active[rows[0]["id"]] = rows[0]
            remember(rows[0])
            remember(line)
    require(set(active) == set(map(str, fields["sales_order_ids"])), "intake_scope_changed")
    visible = policy.source_policy()
    global_policy = source.query("policy", {})["records"]
    require(len(global_policy) == 1 and all(global_policy[0]["fields"].get(k) == visible[k]
            for k in ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")),
            "intake_policy_changed")
    remember(global_policy[0])
    missing = []
    for identity in sorted(active):
        rows = source.query("policy", {"order_id": identity})["records"]
        require(len(rows) == 1 and rows[0]["fields"] == visible["orders"][identity],
                "intake_policy_changed")
        require(rows[0]["fields"]["authorized"] is True, "intake_order_unauthorized")
        remember(rows[0])
        if rows[0]["fields"]["budget_cap"] is None:
            missing.append(identity)
    require(len(missing) == 1, "single_missing_budget_required")
    return {"order_id": missing[0], "order_name": active[missing[0]]["fields"]["name"],
            "product_id": product["id"], "supply_id": supply["id"],
            "offer_key": offers[0]["fields"]["offer_key"], "facts": facts,
            "facts_sha256": digest(facts), "template_sha256": digest(template),
            "policy_sha256": policy.digest(), "event_sha256": event["sha256"]}


def compile_policy(template: dict, policy: PartialPolicy, *, order_id: str, amount: float,
                   revision_id: str, receipt_hash: str, now: float) -> TaskContract:
    """Called only after host confirmation; preserve every other policy field."""
    fields = template_fields(template, policy, now)
    orders = policy.payload()["orders"]
    require(order_id in orders and orders[order_id]["budget_cap"] is None,
            "budget_is_not_missing")
    fields["budgets"] = {key: amount if key == order_id else value["budget_cap"]
                          for key, value in orders.items()}
    fields["contract_id"] = revision_id
    fields["sources"]["budget_confirmation." + order_id] = receipt_hash
    fields["sources"]["parent_policy"] = policy.digest()
    fields["sources"]["task_template"] = digest(template)
    return contract_from_payload(fields)

"""Check field meaning against trusted observations, not model-written citations."""
from __future__ import annotations

import copy
from typing import Any

from forgeflow.contract import check_request
from forgeflow.investigation.event import bind_event
from forgeflow.investigation.sources import collect_pages
from forgeflow.planning.live import LiveInputError, validate_request
from forgeflow.recovery.storage import digest


def issue(code: str, field: str, **details) -> dict:
    return {"code": code, "field": field, **details}


def current_record(source, record: dict) -> dict | None:
    entity, identity = record["entity"], record["id"]
    if entity == "policy":
        result = source.query("policy", {})
    elif entity == "order_policy":
        result = source.query("policy", {"order_id": identity})
    else:
        result = source.query("record", {"entity": entity, "id": identity})
    return next((r for r in result["records"]
                 if r["entity"] == entity and r["id"] == identity), None)


def record_value(record: dict, field: str):
    return record.get("id") if field == "id" else record.get("fields", {}).get(field)


def _fields(request: dict) -> dict[str, tuple[Any, str, str, str | None]]:
    """Request leaf -> (value, source entity, source field, required record id)."""
    result = {
        "target_product_code": (request["target_product_code"], "product", "default_code", None),
        "anchor_date": (request["anchor_date"], "policy", "anchor_date", None),
        "margin": (request["margin"], "policy", "margin", None),
        "disruption.kind": (request["disruption"]["kind"], "policy", "task_type", None),
        "disruption.vendor_ref": (request["disruption"].get("vendor_ref"), "supplier", "ref", None),
    }
    if "offer_key" in request["disruption"]:
        result["disruption.offer_key"] = (
            request["disruption"]["offer_key"], "supplier_offer", "offer_key", None)
    for index, identity in enumerate(request["sales_order_ids"]):
        result[f"sales_order_ids.{index}"] = (str(identity), "sales_order", "id", str(identity))
        result[f"sales_order_budget_caps.{identity}"] = (
            request["sales_order_budget_caps"][str(identity)], "order_policy", "budget_cap", str(identity))
    return result


def related(source, entity: str, identity: str) -> list[dict]:
    return collect_pages(source, "relations", {"entity": entity, "id": identity})


def supply_products(source, target, remember):
    """The target and its directly evidenced components, without a hidden SKU."""
    products = {target["id"]: target}
    for bom in related(source, "product", target["id"]):
        if bom["entity"] != "bom" or not bom["fields"]["active"]:
            continue
        remember(bom)
        if (bom["fields"]["type"] != "normal"
                or bom["fields"]["product_id"] not in (None, target["id"])):
            continue
        for line in related(source, "bom", bom["id"]):
            if line["entity"] != "bom_line":
                continue
            remember(line)
            found = source.query("record", {"entity": "product", "id": line["fields"]["product_id"]})["records"]
            if found:
                remember(found[0])
                products[found[0]["id"]] = found[0]
    return products


def component_target_unambiguous(source, component, target, remember):
    """A component used by several active finished demands cannot imply one target."""
    active_parents = set()
    for line in related(source, "product", component["id"]):
        if line["entity"] != "bom_line":
            continue
        remember(line)
        boms = source.query("record", {"entity": "bom", "id": line["fields"]["bom_id"]})["records"]
        if not boms or not boms[0]["fields"]["active"]:
            continue
        remember(boms[0])
        for parent in related(source, "bom", boms[0]["id"]):
            if parent["entity"] != "product":
                continue
            remember(parent)
            for demand in related(source, "product", parent["id"]):
                if (demand["entity"] != "sales_order_line"
                        or demand["fields"]["quantity"] <= demand["fields"]["delivered_quantity"]):
                    continue
                remember(demand)
                orders = source.query("record", {"entity": "sales_order", "id": demand["fields"]["order_id"]})["records"]
                if orders:
                    remember(orders[0], ("state", "commitment_date"))
                if orders and orders[0]["fields"]["state"] in {"sale", "done"}:
                    active_parents.add(parent["id"])
    return active_parents == {target["id"]}


def validate_draft(draft: dict, observations: dict, source, contract, *, event=None) -> dict:
    """Return safe issues; never project expected values from the hidden contract."""
    issues: list[dict] = []
    facts: dict[str, dict] = {}
    identity_verified = False

    def remember(record, fields=None):
        selected = record["fields"] if fields is None else {
            field: record["fields"].get(field) for field in fields}
        key = record["entity"] + ":" + record["id"]
        prior = facts.setdefault(key, {"entity": record["entity"], "id": record["id"], "fields": {}})
        prior["fields"].update(selected)
    if not isinstance(draft, dict) or set(draft) - {
            "request", "evidence", "supply_observation_id", "reason", "candidates", "unresolved"}:
        return {"valid": False, "issues": [issue("invalid_draft", "draft")]}
    candidates = draft.get("candidates", {})
    unresolved = draft.get("unresolved", [])
    if (not isinstance(candidates, dict) or len(candidates) > 20
            or any(not isinstance(field, str) or not 1 <= len(field) <= 150
                   or not isinstance(ids, list) or len(ids) > 20
                   or any(not isinstance(identity, str) or identity not in observations for identity in ids)
                   for field, ids in candidates.items())):
        return {"valid": False, "issues": [issue("invalid_candidates", "candidates",
                hint="Map request field paths to returned observation_id arrays; omit candidates when resolved.")]}
    if (not isinstance(unresolved, list) or len(unresolved) > 20
            or any(not isinstance(item, dict) or set(item) != {"field", "reason"}
                   or any(not isinstance(v, str) or not 1 <= len(v) <= 500 for v in item.values())
                   for item in unresolved)):
        return {"valid": False, "issues": [issue("invalid_unresolved", "unresolved")]}
    issues.extend(issue("unresolved_field", item["field"]) for item in unresolved)
    request = draft.get("request")
    if not isinstance(request, dict):
        return {"valid": False, "issues": [issue("missing_request", "request")]}
    required = {"target_product_code", "sales_order_ids", "anchor_date", "margin",
                "sales_order_budget_caps", "disruption"}
    missing = sorted(field for field in required if request.get(field) is None)
    if missing:
        return {"valid": False, "issues": issues + [issue("missing_field", name) for name in missing]}
    try:
        request = validate_request(copy.deepcopy(request))
    except (LiveInputError, TypeError, ValueError):
        return {"valid": False, "issues": [issue("invalid_request", "request")]}
    if request["disruption"]["kind"] != "supplier_cancellation":
        return {"valid": False, "issues": [issue("unsupported_disruption", "disruption.kind")]}
    references = draft.get("evidence")
    if not isinstance(references, list) or len(references) > 256:
        return {"valid": False, "issues": [issue("invalid_evidence", "evidence")]}
    refs = {}
    for ref in references:
        if (not isinstance(ref, dict) or set(ref) != {"field", "observation_id", "source_field"}
                or not all(isinstance(v, str) for v in ref.values()) or ref["field"] in refs):
            return {"valid": False, "issues": [issue("invalid_evidence", "evidence")]}
        refs[ref["field"]] = ref
    expected = _fields(request)
    if set(refs) - set(expected):
        issues.append(issue("unexpected_evidence_field", "evidence"))
    bound = {}
    for field, (value, entity, source_field, identity) in expected.items():
        ref = refs.get(field)
        observation = observations.get(ref["observation_id"]) if ref else None
        if not observation:
            issues.append(issue("missing_evidence", field))
            continue
        record = observation["record"]
        if (record["entity"] != entity or ref["source_field"] != source_field
                or (identity is not None and record["id"] != identity)):
            issues.append(issue("wrong_evidence_source", field))
            continue
        observed = record_value(record, source_field)
        if observed != value or isinstance(observed, bool) != isinstance(value, bool):
            issues.append(issue("evidence_value_mismatch", field))
            continue
        fresh = current_record(source, record)
        critical = {"sales_order": ("state", "commitment_date"),
                    "supplier_offer": ("supplier_id", "product_id", "product_template_id", "offer_key")}
        if (fresh is None or record_value(fresh, source_field) != observed
                or any(fresh["fields"].get(key) != record["fields"].get(key)
                       for key in critical.get(entity, ()))):
            issues.append(issue("stale_evidence", field))
            continue
        if entity == "order_policy" and fresh["fields"].get("authorized") is not True:
            issues.append(issue("order_not_authorized", field))
            continue
        if entity == "sales_order" and fresh["fields"].get("state") != "sale":
            issues.append(issue("order_not_active", field))
            continue
        bound[field] = fresh
        remember(fresh, set(critical.get(entity, ())) | ({source_field} - {"id"}))

    product = bound.get("target_product_code")
    supplier = bound.get("disruption.vendor_ref")
    offer = bound.get("disruption.offer_key")
    supply_identity = draft.get("supply_observation_id", "")
    supply_observation = observations.get(supply_identity) if isinstance(supply_identity, str) else None
    if not supply_observation or supply_observation["record"]["entity"] != "purchase_order":
        issues.append(issue("missing_supply_evidence", "disruption"))
    elif product and supplier:
        supply = current_record(source, supply_observation["record"])
        if (supply is None or supply["fields"].get("supplier_id") != supplier["id"]
                or supply["fields"].get("state") not in {"purchase", "done"}):
            issues.append(issue("supply_binding_changed", "disruption"))
        elif any(supply["fields"].get(key) != supply_observation["record"]["fields"].get(key)
                 for key in ("supplier_id", "state", "origin")):
            issues.append(issue("stale_supply_evidence", "disruption"))
        else:
            remember(supply, ("supplier_id", "state", "origin"))
            try:
                lines = related(source, "purchase_order", supply["id"])
                for line in lines:
                    if line["entity"] == "purchase_order_line":
                        remember(line)
                allowed_products = supply_products(source, product, remember)
                relevant = [r for r in lines if r["entity"] == "purchase_order_line"
                            and r["fields"].get("order_id") == supply["id"]
                            and r["fields"].get("product_id") in allowed_products
                            and r["fields"].get("quantity", 0) > r["fields"].get("received_quantity", 0)]
                if not relevant:
                    issues.append(issue("supply_product_unproven", "disruption"))
                elif event is None:
                    issues.append(issue("event_required", "disruption"))
                else:
                    candidates = {row["fields"]["product_id"] for row in relevant}
                    results = {identity: bind_event(event, source, supplier, supply,
                                                   allowed_products[identity], remember)
                               for identity in sorted(candidates)}
                    matched = [identity for identity, error in results.items() if error is None]
                    if len(matched) != 1:
                        issues.append(issue(next(iter(results.values())) or "event_supply_ambiguous", "disruption"))
                    else:
                        supplied = allowed_products[matched[0]]
                        identity_verified = True
                        disruption = request["disruption"]
                        if "purchase_order_id" in disruption and (
                                int(supply["id"]) != disruption["purchase_order_id"] or not any(
                                    int(row["id"]) == disruption["purchase_line_id"]
                                    and row["fields"]["product_id"] == supplied["id"] for row in relevant)):
                            issues.append(issue("cancellation_subject_changed", "disruption"))
                            identity_verified = False
                        if supplied["id"] != product["id"] and not component_target_unambiguous(
                                source, supplied, product, remember):
                            identity_verified = False
                            issues.append(issue("component_target_ambiguous", "target_product_code"))
                        prices = {row["fields"]["unit_price"] for row in relevant
                                  if row["fields"]["product_id"] == supplied["id"]}
                        matching_offers = []
                        for candidate in related(source, "product", supplied["id"]):
                            if candidate["entity"] != "supplier_offer":
                                continue
                            remember(candidate)
                            if (candidate["fields"]["supplier_id"] == supplier["id"]
                                    and candidate["fields"]["product_id"] == supplied["id"]
                                    and len(prices) == 1
                                    and abs(candidate["fields"]["unit_price"] - next(iter(prices))) < 0.005):
                                matching_offers.append(candidate)
                        if len(matching_offers) != 1:
                            identity_verified = False
                            issues.append(issue("supply_offer_ambiguous", "disruption.offer_key"))
                        elif offer and offer["id"] != matching_offers[0]["id"]:
                            identity_verified = False
                            issues.append(issue("offer_supply_mismatch", "disruption.offer_key"))
                        if offer and (offer["fields"].get("supplier_id") != supplier["id"]
                                      or offer["fields"].get("product_id") != supplied["id"]
                                      or offer["fields"].get("product_template_id") != supplied["fields"]["product_template_id"]):
                            identity_verified = False
                            issues.append(issue("offer_binding_mismatch", "disruption.offer_key"))
                        if "offer_key" in request["disruption"] and offer is None:
                            identity_verified = False
            except ValueError:
                identity_verified = False
                issues.append(issue("scope_query_incomplete", "disruption"))

    if product:
        try:
            records = related(source, "product", product["id"])
            for record in records:
                if record["entity"] in {"sales_order_line", "purchase_order_line", "supplier_offer"}:
                    remember(record)
            order_ids = {r["fields"]["order_id"] for r in records
                         if r["entity"] == "sales_order_line"
                         and r["fields"].get("product_id") == product["id"]
                         and r["fields"].get("quantity", 0) > r["fields"].get("delivered_quantity", 0)}
            active = []
            for identity in sorted(order_ids, key=int):
                found = source.query("record", {"entity": "sales_order", "id": identity})["records"]
                if found:
                    remember(found[0], ("state", "commitment_date"))
                if found and found[0]["fields"].get("state") in {"sale", "done"}:
                    active.append(int(identity))
            missing = sorted(set(active) - set(request["sales_order_ids"]))
            if missing:
                issues.append(issue("unselected_active_demand", "sales_order_ids",
                                    missing_sales_order_ids=missing[:20], complete=len(missing) <= 20))
        except ValueError:
            issues.append(issue("scope_query_incomplete", "sales_order_ids"))
    # Even a mismatch code can eliminate an otherwise plausible candidate.
    # Resolve event/target/offer identity from visible facts before consulting
    # the complete hidden reference; public evidence/authorization issues above
    # remain available throughout investigation.
    if contract is None:
        issues.append(issue("complete_policy_required", "policy"))
    elif identity_verified:
        policy_report = check_request(request, contract)
        for problem in policy_report["issues"]:
            issues.append(issue(problem["code"], problem["field"]))
    return {"valid": not issues, "issues": issues, "request_sha256": digest(request),
            "facts_sha256": digest(facts), "verification_facts": facts,
            "request": request if not issues else None}

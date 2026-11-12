"""Development case labels are checked against real evidence and planning outcomes.

The deterministic driver is an integration control, not an agent evaluation.
No held-out cases, reference files, model calls, or mocked planner are used.
"""

import hashlib
import json
from collections import Counter

import pytest
from test_investigation_fixtures import real_bundle as real_bundle

from forgeflow.investigation.fixtures import (
    DEVELOPMENT_DATASET_VERSION,
    FixtureConnection,
    development_cases,
)
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.service import InvestigationError, InvestigationService
from forgeflow.investigation.sources import OdooSource
from forgeflow.recovery.storage import digest, read_json


def service_for(case, root, bundle):
    connection = FixtureConnection(case.records)
    policy = case.policy.source_policy() if isinstance(case.policy, PartialPolicy) else case.policy
    source = OdooSource(connection, policy)
    service = InvestigationService(
        source=source, contract=case.policy, store=root / "store", bundle=bundle,
        connection_factory=lambda: connection,
        event={"text": case.event, "sha256": hashlib.sha256(case.event.encode()).hexdigest()},
    )
    return connection, service


def observed_draft(case, service, *, initial_order_only=False, invent_budget=False):
    """Read facts through real tools. For the ambiguity negative control, try one PO."""
    def query(kind, arguments):
        return service.dispatch(case.case_id, "query", {"kind": kind, "arguments": arguments})

    def record(entity, identity):
        return query("record", {"entity": entity, "id": identity})["records"][0]

    def related(entity, identity):
        rows, cursor = [], None
        while True:
            result = query("relations", {"entity": entity, "id": identity, "limit": 50, "cursor": cursor})
            rows.extend(result["records"])
            if result["complete"]:
                return rows
            cursor = result["next_cursor"]

    matches = query("entities", {"entity": "purchase_order", "query": "PO-CANCEL"})["records"]
    assert len(matches) == 1
    purchase = matches[0]
    supplier = record("supplier", purchase["fields"]["supplier_id"])
    line = next(row for row in related("purchase_order", purchase["id"])
                if row["entity"] == "purchase_order_line")
    product = record("product", line["fields"]["product_id"])
    relations = related("product", product["id"])
    orders = {}
    for row in relations:
        if row["entity"] != "sales_order_line":
            continue
        if row["fields"]["quantity"] <= row["fields"]["delivered_quantity"]:
            continue
        identity = row["fields"]["order_id"]
        order = record("sales_order", identity)
        if order["fields"]["state"] in {"sale", "done"}:
            orders[int(identity)] = order
    if initial_order_only:
        orders = {min(orders): orders[min(orders)]}
    policy = query("policy", {})["records"][0]
    request = {"target_product_code": product["fields"]["default_code"],
               "sales_order_ids": sorted(orders), "anchor_date": policy["fields"]["anchor_date"],
               "margin": policy["fields"]["margin"], "sales_order_budget_caps": {},
               "disruption": {"kind": policy["fields"]["task_type"], "vendor_ref": supplier["fields"]["ref"]}}
    evidence = []

    def bind(field, observed, source_field):
        evidence.append({"field": field, "observation_id": observed["observation_id"],
                         "source_field": source_field})

    bind("target_product_code", product, "default_code")
    bind("anchor_date", policy, "anchor_date")
    bind("margin", policy, "margin")
    bind("disruption.kind", policy, "task_type")
    bind("disruption.vendor_ref", supplier, "ref")
    offers = [row for row in relations if row["entity"] == "supplier_offer"
              and row["fields"]["supplier_id"] == supplier["id"]
              and row["fields"]["unit_price"] == line["fields"]["unit_price"]]
    assert len(offers) == 1
    request["disruption"]["offer_key"] = offers[0]["fields"]["offer_key"]
    bind("disruption.offer_key", offers[0], "offer_key")
    for index, identity in enumerate(sorted(orders)):
        authorized = query("policy", {"order_id": identity})["records"][0]
        budget = authorized["fields"]["budget_cap"]
        request["sales_order_budget_caps"][str(identity)] = 2000 if budget is None and invent_budget else budget
        bind(f"sales_order_ids.{index}", orders[identity], "id")
        bind(f"sales_order_budget_caps.{identity}", authorized, "budget_cap")
    return {"request": request, "evidence": evidence, "supply_observation_id": purchase["observation_id"],
            "reason": "Deterministic development integration control; no model evaluation."}


def test_development_set_is_exactly_eight_with_fixed_outcome_mix():
    cases = development_cases()
    assert len(cases) == len({case.case_id for case in cases}) == 8
    assert all(case.case_id.startswith("dev-") for case in cases)
    assert Counter(case.oracle["expected_status"] for case in cases) == {
        "prepared": 4, "needs_clarification": 2, "infeasible": 1, "unsupported": 1,
    }
    assert DEVELOPMENT_DATASET_VERSION == "investigation-dev-v1"
    frozen = {
        "dev-01-direct": "4c40eda4f5e9a9e996ffa29665abb6f9a162a132861d73bc50124a6236e96daa",
        "dev-02-duplicate-supplier": "5aea4b479332d0d910703ce152caaed522b0236b3a362597e82fbc93c03cf4f7",
        "dev-03-offer-identity": "00dab2e28d26dd0cbcef5f7067733903bc9db1c5202ddfe2b3da312a429ac89e",
        "dev-04-demand-scope": "4391f6b4859707eb61b68fceb15cb427d78ced808784b138eb3da8e7b6701227",
        "dev-05-missing-budget": "6ef99397374df2eec962e11fa389621dc337b42f8f3fcdd3920c430143133937",
        "dev-06-ambiguous-event": "724d59e050258eca4c64bb19996bec882a7aed3134374ba835ad9cd449819b71",
        "dev-07-infeasible": "afd8962b0636423898b8baf6fad9536361f3c6a377ee7c7039325b9bc39537d4",
        "dev-08-multilevel-unsupported": "bf63ddc3ca808d6123cd82addab7217cd67fc0255ce99dd083489cac45374661",
    }
    actual = {case.case_id: digest({"case_id": case.case_id, "event": case.event,
                                   "records": case.records, "policy": case.policy.payload(),
                                   "oracle": case.oracle}) for case in cases}
    assert actual == frozen, "Dataset changes require an explicit version/spec review"


@pytest.mark.parametrize("case", development_cases()[:4], ids=lambda case: case.case_id)
def test_four_prepared_cases_pass_actual_investigation_and_recovery(case, real_bundle, tmp_path):
    connection, service = service_for(case, tmp_path, real_bundle)
    before = connection.fingerprint()
    draft = observed_draft(case, service)
    checked = service.dispatch(case.case_id, "validate", {"draft": draft})
    assert checked["valid"] is True, checked
    result = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
    assert result["status"] == "prepared", result
    assert connection.fingerprint() == before and not connection.write_attempts
    review = read_json(service.root / "review.json")
    assert review["bindings_verified"] is True
    assert review["request_sha256"] == digest(draft["request"])
    path = service.store / result["prepared"]["plan_id"]
    manifest = read_json(path / "manifest.json")
    assert digest(manifest) == path.name
    assert manifest["artifacts"]["request"] == digest(draft["request"])
    plan = read_json(path / "plan.json")
    assert plan["trace"]["solve_calls"]
    assert all(call["status"] in {"OPTIMAL", "FEASIBLE"} for call in plan["trace"]["solve_calls"])
    if case.case_id == "dev-03-offer-identity":
        assert draft["request"]["disruption"]["offer_key"] == case.oracle["required_offer_key"]
    if case.case_id == "dev-04-demand-scope":
        assert draft["request"]["sales_order_ids"] == [1, 2]


def test_scope_case_rejects_partial_request_then_prepares_revised_real_request(real_bundle, tmp_path):
    case = development_cases()[3]
    connection, service = service_for(case, tmp_path, real_bundle)
    first = observed_draft(case, service, initial_order_only=True)
    rejected = service.dispatch(case.case_id, "validate", {"draft": first})
    assert rejected["valid"] is False
    assert any(issue["code"] == "unselected_active_demand" and issue["missing_sales_order_ids"] == [2]
               for issue in rejected["issues"])
    second = observed_draft(case, service)
    checked = service.dispatch(case.case_id, "validate", {"draft": second})
    assert checked["valid"] is True
    result = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
    assert result["status"] == "prepared"
    assert result["draft_revision"] == 2
    assert result["usage"]["prepares"] == 1
    assert not connection.write_attempts


def test_missing_budget_is_real_partial_policy_and_cannot_be_guessed(real_bundle, tmp_path):
    case = development_cases()[4]
    assert isinstance(case.policy, PartialPolicy)
    assert case.contract is None
    assert case.policy.payload()["orders"]["1"]["budget_cap"] is None
    connection, service = service_for(case, tmp_path, real_bundle)
    before = connection.fingerprint()
    draft = observed_draft(case, service, invent_budget=True)
    rejected = service.dispatch(case.case_id, "validate", {"draft": draft})
    assert rejected["valid"] is False
    assert any(issue["field"] == "sales_order_budget_caps.1" for issue in rejected["issues"])
    with pytest.raises(InvestigationError):
        service.dispatch(case.case_id, "prepare", {"draft_id": "0" * 64})
    result = service.dispatch(case.case_id, "finish", {
        "status": "needs_clarification", "reason": "budget_not_authorized",
        "question": "What budget cap is authorized for sales order SO-1?",
    })
    assert result["status"] == "needs_clarification" and result["usage"]["prepares"] == 0
    assert connection.fingerprint() == before
    assert not list(service.store.glob("*/manifest.json"))


def test_irreducible_event_ambiguity_rejects_a_lucky_contract_guess(real_bundle, tmp_path):
    case = development_cases()[5]
    connection, service = service_for(case, tmp_path, real_bundle)
    draft = observed_draft(case, service)
    rejected = service.dispatch(case.case_id, "validate", {"draft": draft})
    assert rejected["valid"] is False
    assert any(issue["code"] == "event_supply_ambiguous" for issue in rejected["issues"])
    result = service.dispatch(case.case_id, "finish", {
        "status": "needs_clarification", "reason": "event_supply_ambiguous",
        "question": "Does the cancellation concern PO-CANCEL or PO-OTHER?",
    })
    assert result["status"] == "needs_clarification"
    assert result["usage"]["prepares"] == 0
    assert not connection.write_attempts


@pytest.mark.parametrize("case", development_cases()[6:], ids=lambda case: case.case_id)
def test_infeasible_and_unsupported_are_proven_by_the_real_pipeline(case, real_bundle, tmp_path):
    connection, service = service_for(case, tmp_path, real_bundle)
    before = connection.fingerprint()
    checked = service.dispatch(case.case_id, "validate", {"draft": observed_draft(case, service)})
    assert checked["valid"] is True, checked
    result = service.dispatch(case.case_id, "prepare", {"draft_id": checked["draft_id"]})
    assert result["status"] == case.oracle["expected_status"], result
    assert result["prepared"] is None
    assert result["usage"]["prepares"] == 1
    assert connection.fingerprint() == before and not connection.write_attempts
    assert not (service.root / "review.json").exists()
    if case.oracle["expected_status"] == "infeasible":
        assert result["terminal"]["reason"] == "planning_model_infeasible"
        attempt = read_json(service.root / "state.json")["attempts"][-1]
        proof = attempt["solver_proof"]
        assert digest(proof) == attempt["solver_proof_sha256"]
        assert proof["request_sha256"] == checked["request_sha256"]
        assert hashlib.sha256(json.dumps(proof["blueprint"], sort_keys=True,
                                         allow_nan=False).encode()).hexdigest() == proof["blueprint_sha256"]
        assert proof["trace"]["solve_calls"][-1]["status"] == "INFEASIBLE"
    else:
        assert result["terminal"]["reason"] == "unsupported_recovery_scope"

"""Evidence admission rejects true-but-irrelevant facts and stale observations."""

import copy
import json

import pytest
from investigation_support import (
    EVENT,
    bind_offer,
    make_case,
    make_draft,
    observe,
    source_record,
)

from forgeflow.investigation.evidence import validate_draft
from forgeflow.recovery.storage import digest


def codes(report):
    return {item["code"] for item in report["issues"]}


def test_complete_evidence_is_valid_and_canonical_request_is_bound():
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is True
    assert report["issues"] == []
    assert report["request"] == request
    assert report["request_sha256"] == digest(request)


def test_sales_value_cannot_be_used_as_budget_even_when_numbers_are_equal():
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    sales_line = source.query("record", {"entity": "sales_order_line", "id": 300})["records"][0]
    assert sales_line["fields"]["unit_price"] == request["sales_order_budget_caps"]["30"]
    identity = observe(observations, sales_line)
    ref = next(ref for ref in draft["evidence"] if ref["field"] == "sales_order_budget_caps.30")
    ref.update(observation_id=identity, source_field="unit_price")
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    assert {"code": "wrong_evidence_source", "field": "sales_order_budget_caps.30"} in report["issues"]
    assert report["request"] is None


@pytest.mark.parametrize("field,other_entity,other_id,source_field", [
    ("sales_order_budget_caps.30", "order_policy", 31, "budget_cap"),
    ("sales_order_ids.0", "sales_order", 31, "id"),
    ("target_product_code", "supplier", 1, "ref"),
])
def test_citations_cannot_cross_entity_or_order_identity(field, other_entity, other_id, source_field):
    source, contract, request = make_case((30, 31))
    draft, observations = make_draft(source, request)
    other = source.query("record", {"entity": other_entity, "id": other_id})["records"][0]
    ref = next(ref for ref in draft["evidence"] if ref["field"] == field)
    ref.update(observation_id=observe(observations, other), source_field=source_field)
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    assert {"code": "wrong_evidence_source", "field": field} in report["issues"]


@pytest.mark.parametrize("entity,identity,key,new_value,expected_code", [
    ("supplier", 1, "ref", "V-CHANGED", "stale_evidence"),
    ("product", 10, "default_code", "RENAMED", "stale_evidence"),
    ("sales_order", 30, "state", "cancel", "stale_evidence"),
    ("sales_order", 30, "commitment_date", "2026-01-09 12:00:00", "stale_evidence"),
    ("purchase_order", 20, "supplier_id", "2", "supply_binding_changed"),
    ("purchase_order", 20, "state", "cancel", "supply_binding_changed"),
    ("purchase_order", 20, "origin", "SO-31", "stale_supply_evidence"),
    ("purchase_order_line", 200, "product_id", "11", "supply_product_unproven"),
    ("purchase_order_line", 200, "received_quantity", 5, "supply_product_unproven"),
])
def test_changed_key_business_facts_are_rejected(entity, identity, key, new_value, expected_code):
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    source_record(source, entity, identity)["fields"][key] = new_value
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    assert expected_code in codes(report)


@pytest.mark.parametrize("change", ["budget", "authorization"])
def test_policy_changes_are_checked_again_at_admission(change):
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    source._policy["orders"]["30"] = (
        {"authorized": True, "budget_cap": 900} if change == "budget"
        else {"authorized": False, "budget_cap": None})
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    assert "stale_evidence" in codes(report) or "order_not_authorized" in codes(report)


@pytest.mark.parametrize("state", ["sale", "done"])
def test_missing_active_orders_have_record_derived_feedback_without_policy_answers(state):
    source, contract, request = make_case((30, 31))
    source_record(source, "sales_order", 31)["fields"]["state"] = state
    request["sales_order_ids"] = [30]
    request["sales_order_budget_caps"] = {"30": 1000}
    draft, observations = make_draft(source, request)
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    feedback = next(item for item in report["issues"] if item["code"] == "unselected_active_demand")
    assert feedback == {"code": "unselected_active_demand", "field": "sales_order_ids",
                        "missing_sales_order_ids": [31], "complete": True}
    assert all(set(item) <= {"code", "field", "missing_sales_order_ids", "complete"}
               for item in report["issues"])
    assert '"expected"' not in json.dumps(report)
    assert '"budgets"' not in json.dumps(report)


def test_missing_contract_order_is_not_leaked_if_not_present_in_business_records():
    source, contract, request = make_case((30, 31))
    source.records = [row for row in source.records if not (
        row["entity"] == "sales_order" and row["id"] == "31"
        or row["entity"] == "sales_order_line" and row["fields"]["order_id"] == "31")]
    request["sales_order_ids"] = [30]
    request["sales_order_budget_caps"] = {"30": 1000}
    draft, observations = make_draft(source, request)
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    assert {"code": "order_scope_missing", "field": "sales_order_ids"} in report["issues"]
    assert "unselected_active_demand" not in codes(report)
    assert all("missing_sales_order_ids" not in item for item in report["issues"])


@pytest.mark.parametrize("offer_id", [1001, 1002])
def test_true_offer_from_another_supplier_or_product_cannot_support_disruption(offer_id):
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    bind_offer(draft, observations, source, offer_id)
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False, "A real but unrelated supplier offer must not authorize this request"
    assert any(item["field"].startswith("disruption") for item in report["issues"])


def test_matching_offer_can_support_disruption():
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    bind_offer(draft, observations, source, 1000)
    assert validate_draft(draft, observations, source, contract, event=EVENT)["valid"] is True


def test_incomplete_or_cyclic_relations_never_imply_complete_scope():
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)

    class Truncated:
        def query(self, kind, arguments):
            if kind == "relations":
                return {"records": [], "complete": False, "next_cursor": "same-cursor"}
            return source.query(kind, arguments)

    report = validate_draft(draft, observations, Truncated(), contract, event=EVENT)
    assert report["valid"] is False
    assert "scope_query_incomplete" in codes(report)


def test_every_request_field_needs_evidence_and_raw_model_claims_are_not_sources():
    source, contract, request = make_case()
    draft, observations = make_draft(source, request)
    draft["evidence"] = []
    report = validate_draft(draft, observations, source, contract, event=EVENT)
    assert report["valid"] is False
    assert "missing_evidence" in codes(report)
    forged = copy.deepcopy(draft)
    forged["confirmed_facts"] = {"budget": 1000}
    assert validate_draft(forged, observations, source, contract, event=EVENT) == {
        "valid": False, "issues": [{"code": "invalid_draft", "field": "draft"}],
    }

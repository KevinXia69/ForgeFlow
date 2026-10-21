from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from forgeflow.contract import (
    ContractError,
    build_contract,
    check_changes,
    check_request,
    contract_from_payload,
)

ANCHOR = "2026-10-21"
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def supplier_contract(**overrides):
    values = dict(
        contract_id="A-supplier-v1",
        task_id="2280_medium_repair_plan_medium",
        task_type="supplier_cancellation",
        product_code="P27637E67E7-LED-CRS-009",
        anchor_date=ANCHOR,
        margin=0.275,
        margin_basis="portfolio_new_spend_margin_at_selling_price",
        budgets={"1": 3735.0, "2": 4231.0, "3": 4643.0, "4": 6417.0, "5": 6828.0},
        sales_order_ids=(1, 2, 3, 4, 5),
        disruption={
            "kind": "supplier_cancellation",
            "vendor_ref": "r27637E67E7_q02",
            "vendor_name": "Ridgeway Fabrication",
        },
        forbidden_operation_kinds=(),
        original_commitment_policy="keep_commitments_that_still_work_and_minimize_change",
        sources={"budgets": "instruction.md: budgets", "margin": "instruction.md: portfolio margin"},
    )
    values.update(overrides)
    return build_contract(**values)


def supplier_request(**overrides):
    values = {
        "target_product_code": "P27637E67E7-LED-CRS-009",
        "sales_order_ids": [1, 2, 3, 4, 5],
        "anchor_date": ANCHOR,
        "margin": 0.275,
        "sales_order_budget_caps": {
            "1": 3735, "2": 4231, "3": 4643, "4": 6417, "5": 6828,
        },
        "disruption": {"kind": "supplier_cancellation", "vendor_ref": "r27637E67E7_q02"},
    }
    values.update(overrides)
    return values


def codes(report):
    return [issue["code"] for issue in report["issues"]]


def test_matching_request_passes():
    report = check_request(supplier_request(), supplier_contract(), now=NOW)
    assert report["ok"] is True
    assert report["issues"] == []
    assert len(report["contract_sha256"]) == 64


def test_display_name_instead_of_vendor_ref_is_named():
    request = supplier_request(disruption={
        "kind": "supplier_cancellation", "vendor_ref": "Ridgeway Fabrication",
    })
    report = check_request(request, supplier_contract(), now=NOW)
    assert report["ok"] is False
    assert codes(report) == ["identifier_is_display_name"]


def test_unknown_vendor_ref_is_a_mismatch_not_a_name_confusion():
    request = supplier_request(disruption={
        "kind": "supplier_cancellation", "vendor_ref": "r99999E99E9_q99",
    })
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["vendor_ref_mismatch"]


def test_missing_order_is_reported_with_the_missing_ids():
    request = supplier_request(sales_order_ids=[1, 2, 3, 4])
    request["sales_order_budget_caps"].pop("5")
    report = check_request(request, supplier_contract(), now=NOW)
    assert codes(report) == ["order_scope_missing"]
    assert report["issues"][0]["observed"] == [5]


def test_extra_order_is_reported_so_scope_never_expands_silently():
    request = supplier_request(sales_order_ids=[1, 2, 3, 4, 5, 6])
    request["sales_order_budget_caps"]["6"] = 1000
    report = check_request(request, supplier_contract(), now=NOW)
    assert codes(report) == ["order_scope_extra"]
    assert report["issues"][0]["observed"] == [6]


def test_a_tighter_budget_from_the_wrong_field_is_still_a_conflict():
    request = supplier_request()
    request["sales_order_budget_caps"] = {
        "1": 3500, "2": 4231, "3": 4643, "4": 6417, "5": 6828,
    }
    report = check_request(request, supplier_contract(), now=NOW)
    assert codes(report) == ["budget_mismatch"]
    assert report["issues"][0]["expected"] == 3735.0
    assert report["issues"][0]["observed"] == 3500


def test_missing_budget_cap_is_not_treated_as_zero():
    request = supplier_request()
    request["sales_order_budget_caps"].pop("3")
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["budget_missing"]


def test_budget_for_an_order_outside_the_scope_is_reported():
    request = supplier_request()
    request["sales_order_budget_caps"]["9"] = 1000
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["budget_unknown_order"]


@pytest.mark.parametrize("margin", [0.26, 0.3, 0.2751])
def test_margin_rewrite_is_rejected(margin):
    request = supplier_request(margin=margin)
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["margin_mismatch"]


def test_anomaly_type_mismatch_is_rejected():
    request = supplier_request(disruption={
        "kind": "workcenter_outage", "workcenter_code": "P1223565D60-WC01",
    })
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["disruption_type_mismatch"]


def test_expired_contract_is_rejected():
    contract = supplier_contract(expires_at_utc="2026-10-21T11:00:00+00:00")
    report = check_request(supplier_request(), contract, now=NOW)
    assert codes(report) == ["contract_expired"]


def test_anchor_date_mismatch_is_rejected():
    request = supplier_request(anchor_date="2026-10-22")
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["anchor_date_mismatch"]


def test_product_mismatch_is_rejected():
    request = supplier_request(target_product_code="P0000000000-WRONG")
    assert codes(check_request(request, supplier_contract(), now=NOW)) == ["product_mismatch"]


def test_unreadable_request_fields_are_reported_not_raised():
    request = supplier_request(sales_order_ids="1,2,3", sales_order_budget_caps=None)
    report = check_request(request, supplier_contract(), now=NOW)
    assert "order_scope_unusable" in codes(report)
    assert "budget_unusable" in codes(report)


def outage_contract(**overrides):
    values = dict(
        contract_id="B-outage-v1",
        task_id="2299_hard_repair_plan_hard",
        task_type="workcenter_outage",
        product_code="P1223565D60-SOL-CSC-007",
        anchor_date=ANCHOR,
        margin=0.26,
        margin_basis="portfolio_new_spend_margin_at_selling_price",
        budgets={"1": 69765.3, "2": 58137.75},
        sales_order_ids=(1, 2),
        disruption={
            "kind": "workcenter_outage",
            "workcenter_code": "P1223565D60-WC01",
            "workcenter_name": "Assembly Line 1",
        },
        forbidden_workcenters=("P1223565D60-WC01",),
        forbidden_operation_kinds=(),
        original_commitment_policy="cancel_affected_manufacturing_and_repair_the_plan",
        sources={"budgets": "instruction.md: budgets", "workcenter": "instruction.md: outage"},
    )
    values.update(overrides)
    return build_contract(**values)


def test_downed_workcenter_scheduled_by_a_non_cancel_operation_is_rejected():
    changes = {"operations": [
        {"action": "cancel", "after": None, "kind": "manufacturing_order"},
        {"action": "create", "after": {"workcenter_code": "P1223565D60-WC01"},
         "kind": "manufacturing_order"},
    ]}
    report = check_changes(changes, outage_contract())
    assert report["ok"] is False
    assert codes(report) == ["forbidden_workcenter_in_plan"]
    assert "operations.1" in report["issues"][0]["field"]


def test_cancelling_the_downed_workcenter_plan_is_allowed():
    changes = {"operations": [
        {"action": "cancel", "after": None, "kind": "manufacturing_order"},
        {"action": "update", "after": {"workcenter_code": "P1223565D60-WC02"},
         "kind": "manufacturing_order"},
    ]}
    assert check_changes(changes, outage_contract())["ok"] is True


def test_forbidden_operation_kind_is_rejected():
    contract = outage_contract(forbidden_operation_kinds=("purchase_order",))
    changes = {"operations": [
        {"action": "create", "after": {}, "kind": "purchase_order"},
    ]}
    assert codes(check_changes(changes, contract)) == ["forbidden_operation_kind"]


def test_physical_actions_are_not_a_contract_option():
    with pytest.raises(ContractError, match="forbidden_operation_kinds"):
        outage_contract(forbidden_operation_kinds=("receipt_confirmation",))


def test_executor_kinds_contain_no_physical_action():
    from forgeflow.recovery.journal import KINDS
    forbidden = {"receipt_confirmation", "production_done", "inventory_adjustment"}
    assert not (set(KINDS) & forbidden)


def test_unknown_operation_action_is_rejected():
    changes = {"operations": [{"action": "delete", "after": {}, "kind": "manufacturing_order"}]}
    assert "unknown_operation_action" in codes(check_changes(changes, outage_contract()))


def test_contract_round_trips_through_json_and_keeps_its_digest():
    contract = supplier_contract()
    payload = json.loads(json.dumps(contract.payload(), sort_keys=True))
    restored = contract_from_payload(payload)
    assert restored == contract
    assert restored.digest() == contract.digest()


def test_contract_from_payload_rejects_another_schema():
    payload = supplier_contract().payload()
    payload["schema_version"] = 99
    with pytest.raises(ContractError):
        contract_from_payload(payload)


@pytest.mark.parametrize("overrides", [
    {"budgets": {"1": 1.0}},
    {"sales_order_ids": (1, 1)},
    {"margin": 1.2},
    {"task_type": "unknown"},
    {"anchor_date": "21/10/2026"},
    {"sources": {}},
    {"disruption": {"kind": "supplier_cancellation", "vendor_name": "X"}},
    {"expires_at_utc": "2026-10-21T11:00:00"},
])
def test_invalid_contracts_are_refused_at_construction(overrides):
    with pytest.raises(ContractError):
        supplier_contract(**overrides)


def test_digest_changes_when_a_budget_changes():
    first = supplier_contract()
    second = supplier_contract(budgets={**first.budgets, "1": 3736.0})
    assert first.digest() != second.digest()


def test_a_non_object_request_returns_a_report_instead_of_raising():
    report = check_request("not a request", supplier_contract(), now=NOW)
    assert report["ok"] is False
    assert codes(report) == ["request_unusable"]


def test_duplicate_order_ids_are_reported():
    request = supplier_request(sales_order_ids=[1, 1, 2, 3, 4, 5])
    assert "order_scope_duplicate" in codes(check_request(request, supplier_contract(), now=NOW))


def test_a_naive_reference_time_is_a_report_issue_not_a_crash():
    contract = supplier_contract(expires_at_utc="2026-10-21T11:00:00+00:00")
    report = check_request(supplier_request(), contract, now=datetime(2026, 10, 21, 12, 0))
    assert "invalid_reference_time" in codes(report)


def test_non_numeric_budget_keys_do_not_crash_the_check():
    request = supplier_request()
    request["sales_order_budget_caps"]["extra"] = 10
    report = check_request(request, supplier_contract(), now=NOW)
    assert "budget_unknown_order" in codes(report)


def test_non_cancel_operation_without_a_projection_is_reported():
    changes = {"operations": [{"action": "create", "after": None, "kind": "purchase_order"}]}
    assert "operation_missing_projection" in codes(check_changes(changes, outage_contract()))

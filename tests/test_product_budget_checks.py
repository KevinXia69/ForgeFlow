"""Customer-cap semantics, persisted binding and pre-authority refusal; invented data only."""
import copy

import pytest

from forgeflow.product.budget_checks import (
    build_budget_check,
    order_budget_facts,
    project_budget_check,
)
from forgeflow.product.executor import FakeProductExecutor, build_fake_plan
from forgeflow.product.service import ProductCaseError, ProductCaseService
from forgeflow.product.state import (
    AWAITING_APPROVAL,
    ProductCase,
    approval_decision,
    normalize_plan,
)
from forgeflow.recovery.storage import digest


def inputs():
    request = {"sales_order_ids": [1, 2], "sales_order_budget_caps": {"1": "100", "2": "1000"}}
    snapshot = {"models": {"sale.order": [{"id": 1, "name": "SO1"}, {"id": 2, "name": "SO2"}],
        "sale.order.line": [
            {"order_id": [1, "SO1"], "display_type": False, "product_uom_qty": "2.5", "price_unit": "40"},
            {"order_id": 2, "display_type": False, "product_uom_qty": 1, "price_unit": "500"},
        ]}}
    return request, snapshot


def check(request=None, snapshot=None, **keys):
    original_request, original_snapshot = inputs()
    request = original_request if request is None else request
    snapshot = original_snapshot if snapshot is None else snapshot
    return build_budget_check(request, snapshot, plan_id=keys.get("plan_id", "a" * 64),
                              snapshot_sha256=keys.get("snapshot_sha256", digest(snapshot)))


def marked_case():
    case = ProductCase.new("owner", "A")
    plan = build_fake_plan(case.view())
    _, snapshot = inputs()
    plan["snapshot_sha256"] = digest(snapshot)
    plan["checks"].append(check(plan_id=plan["plan_id"]))
    case.plan = normalize_plan(case, plan)
    case.state = AWAITING_APPROVAL
    return case


def test_exact_individual_boundary_and_shared_facts_do_not_mutate_sources():
    request, snapshot = inputs()
    before = copy.deepcopy((request, snapshot))
    result = check(request, snapshot)
    assert result["status"] == "pass"
    assert result["actual"] == result["threshold"] == 2
    assert order_budget_facts(request, snapshot) == [
        {"order_ref": "SO1", "sales_value": "100", "customer_cap": "100", "headroom": "0", "compliant": True},
        {"order_ref": "SO2", "sales_value": "500", "customer_cap": "1000", "headroom": "500", "compliant": True}]
    assert (request, snapshot) == before


@pytest.mark.parametrize("fault", ["missing_cap", "missing_order", "duplicate_order", "missing_line", "duplicate_line",
                                   "missing_display", "negative", "boolean", "infinite", "oversize"])
def test_partial_records_remain_unknown_and_do_not_claim_compliant_count(fault):
    request, snapshot = inputs()
    models = snapshot["models"]
    line = models["sale.order.line"][0]
    if fault == "missing_cap":
        request["sales_order_budget_caps"].pop("1")
    elif fault == "missing_order":
        models["sale.order"].pop(0)
    elif fault == "duplicate_order":
        models["sale.order"].append(copy.deepcopy(models["sale.order"][0]))
    elif fault == "missing_line":
        models["sale.order.line"].pop(0)
    elif fault == "duplicate_line":
        models["sale.order.line"].append(copy.deepcopy(line))
    elif fault == "missing_display":
        line.pop("display_type")
    else:
        line["price_unit"] = {"negative": -1, "boolean": True, "infinite": "Infinity", "oversize": "1e99999"}[fault]
    result = check(request, snapshot)
    assert result["status"] == "unknown"
    assert result["actual"] is None and result["threshold"] == 2


@pytest.mark.parametrize("scope", [None, [], [1, 1], [True, 2], ["1", 2], [-1, 2]])
def test_unestablished_scope_is_unknown(scope):
    request, snapshot = inputs()
    request["sales_order_ids"] = scope
    result = check(request, snapshot)
    assert result["status"] == "unknown" and result["actual"] is result["threshold"] is None


def test_known_overcap_blocks_even_when_another_order_is_missing():
    request, snapshot = inputs()
    request["sales_order_budget_caps"] = {"1": "99.99"}
    result = check(request, snapshot)
    assert result["status"] == "block" and result["actual"] == 0 and result["threshold"] == 2


def test_unbound_snapshot_cannot_supply_new_budget_evidence():
    with pytest.raises(ValueError, match="invalid_budget_check"):
        check(snapshot_sha256="b" * 64)


@pytest.mark.parametrize("fault", ["parent_plan", "parent_snapshot", "kind", "extra", "null_basis", "float_count",
                                   "bool_count", "zero_threshold", "short_pass", "full_block", "warn", "known_unknown", "wrong_code"])
def test_marker_and_status_counts_fail_closed_at_normalization(fault):
    case = marked_case()
    plan = copy.deepcopy(case.plan)
    budget = plan["checks"][-1]
    if fault in {"parent_plan", "parent_snapshot"}:
        plan["plan_id" if fault == "parent_plan" else "snapshot_sha256"] = "b" * 64
    elif fault == "kind":
        budget["basis"]["kind"] = "total_caps"
    elif fault == "extra":
        budget["basis"]["private"] = "CANARY"
    elif fault == "null_basis":
        budget["basis"] = None
    elif fault == "float_count":
        budget["actual"] = 2.0
    elif fault == "bool_count":
        budget["actual"] = True
    elif fault == "zero_threshold":
        budget["actual"] = budget["threshold"] = 0
    elif fault == "short_pass":
        budget["actual"] = 1
    elif fault in {"full_block", "warn", "known_unknown"}:
        budget["status"] = {"full_block": "block", "warn": "warn", "known_unknown": "unknown"}[fault]
    else:
        budget["code"] = "arbitrary"
    with pytest.raises(ValueError, match="invalid_budget_check"):
        normalize_plan(case, plan)
    case.plan = plan
    assert approval_decision(case)["can_approve"] is False


@pytest.mark.parametrize("target", ["current", "history"])
def test_saved_current_and_history_validate_without_rewriting_and_project_same_basis(target):
    case = marked_case()
    if target == "history":
        case.history = [{"version": 1, "plan": copy.deepcopy(case.plan), "reason": "retired"}]
    record = case.to_record()
    restored = ProductCase.from_record(record)
    assert restored.to_record() == record
    selected = restored.view()["plan"] if target == "current" else restored.view()["history"][0]["plan"]
    assert selected["checks"][-1] == case.plan["checks"][-1]
    plan = restored.plan if target == "current" else restored.history[0]["plan"]
    plan["checks"][-1]["basis"]["snapshot_sha256"] = "c" * 64
    for action in (restored.to_record, restored.view):
        with pytest.raises(ValueError, match="invalid_budget_check"):
            action()
    source = record["plan"] if target == "current" else record["history"][0]["plan"]
    source["checks"][-1]["status"] = "unknown"  # known count is inconsistent
    with pytest.raises(ValueError, match="invalid_budget_check"):
        ProductCase.from_record(record)


def test_unmarked_legacy_check_is_not_upgraded_by_its_label_or_recorded_metrics():
    case = marked_case()
    legacy = case.plan["checks"][-1]
    legacy.pop("basis")
    legacy.update(actual="$50.00", threshold="$1,100.00", source="legacy total comparison")
    case.history = [{"version": 1, "plan": copy.deepcopy(case.plan)}]
    record = case.to_record()
    restored = ProductCase.from_record(record)
    assert restored.to_record() == record
    assert restored.view()["plan"]["checks"][-1] == legacy
    assert restored.view()["history"][0]["plan"]["checks"][-1] == legacy
    assert approval_decision(restored)["can_approve"] is True
    projected = project_budget_check(legacy, plan_id=None, snapshot_sha256=None)
    assert projected == legacy and projected is not legacy


@pytest.mark.parametrize("target", ["current", "history"])
@pytest.mark.parametrize("legacy_override", [False, True])
def test_duplicate_budget_cannot_override_recorded_block_at_any_saved_boundary(target, legacy_override):
    case = marked_case()
    case.plan["checks"][-1].update(status="block", actual=0)
    if target == "history":
        case.history = [{"version": 1, "plan": copy.deepcopy(case.plan)}]
    record = case.to_record()
    plan = case.plan if target == "current" else case.history[0]["plan"]
    override = copy.deepcopy(plan["checks"][-1])
    override.update(status="pass", actual=override["threshold"])
    if legacy_override:
        override.pop("basis")
    plan["checks"].append(override)
    for action in (case.to_record, case.view):
        with pytest.raises(ValueError, match="duplicate_check_code"):
            action()
    assert approval_decision(case)["can_approve"] is False
    saved = record["plan"] if target == "current" else record["history"][0]["plan"]
    saved["checks"].append(override)
    with pytest.raises(ValueError, match="duplicate_check_code"):
        ProductCase.from_record(record)


class BudgetExecutor(FakeProductExecutor):
    def __init__(self, *, overcap=False, missing=False):
        super().__init__()
        self.overcap, self.missing = overcap, missing
        self.authorize_calls = 0

    def prepare(self, case):
        plan = super().prepare(case)
        request, snapshot = inputs()
        if self.overcap:
            request["sales_order_budget_caps"]["1"] = "99"
        if self.missing:
            request["sales_order_budget_caps"].pop("2")
        # The formerly misleading total comparison would pass: 50 < 1099.
        plan["new_spend"] = 50
        plan["snapshot_sha256"] = digest(snapshot)
        plan["checks"].append(check(request, snapshot, plan_id=plan["plan_id"]))
        return plan

    def authorize(self, case, plan):
        self.authorize_calls += 1
        raise AssertionError("budget refusal must happen before authority")


@pytest.mark.parametrize(("options", "reason"), [({"overcap": True}, "business_check_blocked"),
                                                ({"missing": True}, "required_checks_unknown")])
def test_individual_budget_refusal_survives_save_reload_before_authorize_or_apply(tmp_path, options, reason):
    executor = BudgetExecutor(**options)
    service = ProductCaseService(tmp_path, executor)
    case = service.create("owner", "A")
    prepared = service.prepare(case["case_id"], "owner")
    budget = prepared["plan"]["checks"][-1]
    assert prepared["plan"]["new_spend"] < 1099
    assert prepared["decision"]["can_approve"] is False
    reloaded = ProductCaseService(tmp_path, executor)
    assert reloaded.get(case["case_id"], "owner")["plan"]["checks"][-1] == budget
    with pytest.raises(ProductCaseError, match=reason):
        reloaded.approve(case["case_id"], "owner", prepared["plan"]["plan_id"])
    assert executor.authorize_calls == executor.apply_calls == 0
    assert reloaded.get(case["case_id"], "owner")["approval"] is None


def test_compliant_marked_plan_can_be_approved_once_and_replayed_unchanged(tmp_path):
    executor = BudgetExecutor()
    executor.authorize = None
    service = ProductCaseService(tmp_path, executor)
    case = service.create("owner", "A")
    prepared = service.prepare(case["case_id"], "owner")
    assert prepared["decision"]["can_approve"] is True
    approved = service.approve(case["case_id"], "owner", prepared["plan"]["plan_id"])
    replay = ProductCaseService(tmp_path, executor).approve(case["case_id"], "owner", prepared["plan"]["plan_id"])
    assert replay == approved and executor.apply_calls == 1
    assert approved["plan"] == prepared["plan"]

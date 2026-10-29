"""Public facts preserve corrected, zero and historical margin meanings."""
import copy
import json

import pytest
from test_decision_metrics import facts
from test_recovery_change_set import stock_margin_fixture

from forgeflow.api.case_context import case_context
from forgeflow.api.case_qa import _headroom, answer_case
from forgeflow.product.decision_metrics import build_decision_metrics
from forgeflow.product.state import _bound_decision_metrics
from forgeflow.recovery.margin import MARGIN_BASIS


def current_plan(*, stock=5, unit_cost=76, raw_price=2):
    inputs, keys = facts(inputs=stock_margin_fixture(stock=stock, unit_cost=unit_cost, raw_price=raw_price))
    metrics = build_decision_metrics(*inputs, **keys)
    ratio = metrics["margin"]["planned_margin_pct"]
    return {**keys, "margin_basis": MARGIN_BASIS, "margin": None if ratio is None else round(float(ratio), 2),
            "decision_metrics": metrics, "costs": inputs[4], "capacity": inputs[5], "order_readiness": inputs[6],
            "actions": [], "new_spend": float(metrics["margin"]["planned_cost"]),
            "checks": [{"code": "margin", "status": "pass", "threshold": "20%",
                        "actual": "not applicable" if ratio is None else ratio + "%"}]}


@pytest.mark.parametrize("lang", ["en", "zh"])
def test_exact_floor_headroom_zero_stays_zero(lang):
    plan = current_plan()
    lines, _ = _headroom(plan, lang)
    text = " ".join(lines)
    assert "20%" in text
    assert "headroom 0 percentage points" in text if lang == "en" else "余量 0 个百分点" in text
    assert "USD 0.00" in text


@pytest.mark.parametrize("question", ["What is the plan margin?", "方案利润率是多少？"])
def test_all_stock_zero_cost_is_not_applicable_in_plan_and_headroom(question):
    plan = current_plan(stock=10, unit_cost=0, raw_price=0)
    case = {"state": "awaiting_approval", "plan": plan}
    before = copy.deepcopy(case)
    reply = answer_case(question, case)
    text = reply["answer"] + " ".join(_headroom(plan, "zh" if "方案" in question else "en")[0])
    assert "not applicable" in text or "不适用" in text
    assert "100%" not in text and "unknown%" not in text and "未知%" not in text
    assert case == before


@pytest.mark.parametrize("fault", ["marker", "ratio", "schema"])
def test_state_refuses_mixed_or_changed_margin_evidence(fault):
    plan = current_plan()
    assert _bound_decision_metrics(plan) == plan["decision_metrics"]
    if fault == "marker":
        del plan["margin_basis"]
    elif fault == "ratio":
        plan["margin"] = 60
    else:
        plan["decision_metrics"]["schema_version"] = 1
    with pytest.raises(ValueError):
        _bound_decision_metrics(plan)


@pytest.mark.parametrize("large", [False, True])
def test_legacy_pass_cannot_become_current_compliance_in_qa_or_context(large):
    plan = {"margin": 60, "checks": [{"code": "margin", "status": "pass", "threshold": "50%"}],
            "actions": [{"summary": "x" * 200}] * (100 if large else 0)}
    case = {"plan": plan, "state": "succeeded"}
    before = copy.deepcopy(case)
    text = answer_case("Explain the plan checks", case)["answer"]
    assert "Historical margin check" in text and "current compliance unverified" in text
    assert "Margin: pass" not in text and "against a 50% threshold" not in text
    context = json.loads(case_context(case))
    assert "legacy margins" in context["margin_check_interpretation"]
    assert case == before

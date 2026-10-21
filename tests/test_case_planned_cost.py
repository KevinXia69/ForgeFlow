"""Cost labels preserve the solver quantity without inventing new purchases or a delta."""
from __future__ import annotations

import copy

import pytest

from forgeflow.api.case_qa import answer_case


@pytest.mark.parametrize("question,label,caveat,margin_basis,budget_basis", [
    ("What does the plan cost?", "Planned cost", "including retained existing documents",
     "not measured post-execution accounting margin", "not a new-purchase authorization limit"),
    ("方案成本是什么？", "方案规划成本", "保留的既有单据", "不是执行后实际毛利率", "不是新增采购授权额度"),
    ("Does the proposal meet the customer budget?", "Planned cost", "including retained existing documents",
     "not measured post-execution accounting margin", "not a new-purchase authorization limit"),
    ("方案是否满足客户预算？", "方案规划成本", "保留的既有单据", "不是执行后实际毛利率", "不是新增采购授权额度"),
])
@pytest.mark.parametrize("amount", [133394.07, 0.0, None])
def test_unchanged_purchase_values_do_not_turn_planning_cost_into_increment(
    question, label, caveat, margin_basis, budget_basis, amount,
):
    # Observed B-shaped trigger: existing PO values stay fixed, no create_po.
    case = {"state": "awaiting_approval", "plan": {
        "new_spend": amount, "cost_delta": None, "margin": 84.9,
        "actions": [{"kind": "update_po", "document": "P1",
                     "before": {"quantity": 38, "unit_price": 95.92},
                     "after": {"quantity": 38, "unit_price": 95.92}}],
        "checks": [{"code": "margin", "status": "pass", "threshold": "26%"},
                   {"code": "budget", "status": "pass", "actual": amount, "threshold": 943302}],
    }}
    before = copy.deepcopy(case)
    reply = answer_case(question, case)
    citations = {row["field"]: row for row in reply["citations"]}
    assert case == before
    assert "New spend is" not in reply["answer"]
    assert "新增支出为" not in reply["answer"]
    assert margin_basis in reply["answer"]
    assert budget_basis in reply["answer"]
    assert "plan.cost_delta" not in citations
    if amount is None:
        assert "plan.new_spend" not in citations
        assert "$0.00" not in reply["answer"]
    else:
        assert citations["plan.new_spend"] == {"field": "plan.new_spend", "label": label, "value": amount}
        assert f"${amount:,.2f}" in reply["answer"]
        assert caveat in reply["answer"]

"""Handwritten expectations for matching identity annotations, never budget amounts."""
from types import SimpleNamespace

import pytest

ACTUAL_QUESTION = (
    "What is the budget cap (positive spending limit) for sales order SO-1 (id=1)? "
    "This is required to validate the supplier_cancellation recovery draft."
)


@pytest.fixture(scope="module")
def grader():
    from forgeflow.investigation import grading
    return grading


def case(identity=1, name="SO-1"):
    return SimpleNamespace(
        oracle={"required_question_fields": [f"sales_order_budget_caps.{identity}"]},
        records={"sale.order": [{"id": identity, "name": name}]},
    )


def test_actual_question_explicit_order_id_is_not_a_proposed_budget(grader):
    result = grader.grade_clarification(case(), ACTUAL_QUESTION)
    assert result["verdict"] == "correct"
    assert result["matched_fields"] == ["sales_order_budget_caps.1"]


@pytest.mark.parametrize("question", [
    "What budget cap is authorized for SO-1 (ID: 1)?",
    "What budget cap is authorized for SO-1 (id = 1)?",
    "What budget cap is authorized for SO-1 (ID 1)?",
    "What budget cap is authorized for SO-1 (编号=1)?",
    "请提供销售订单 SO-1（编号＝1）的授权预算上限。",
    "请提供 SO-1（ID：1）的预算上限。",
    "What budget cap is authorized for sales order ID=1?",
    "请提供销售订单编号=1的授权预算上限。",
    "请提供销售订单编号＝1的授权预算上限。",
    "What budget cap is authorized for SO-1?",
    "What budget cap is authorized for sales order ID 1?",
    "What budget cap is authorized for SO-1 (sales order ID 1)?",
])
def test_exact_explicit_identity_equivalents(grader, question):
    assert grader.grade_clarification(case(), question)["verdict"] == "correct"


@pytest.mark.parametrize("amount", ["1", "600", "650", "1.50", "0.01"])
@pytest.mark.parametrize("identity", ["SO-1 (id=1)", "sales order ID=1"])
def test_equal_id_and_other_proposed_budget_amounts_are_not_erased(grader, identity, amount):
    result = grader.grade_clarification(case(), f"Please confirm budget cap {amount} for {identity}?")
    assert result["verdict"] == "incorrect"
    assert result["reason"] == "agent_proposed_missing_budget"


@pytest.mark.parametrize("question", [
    "What budget cap is authorized for SO-1 (id=2)?",
    "What budget cap is authorized for SO-1 (id=11)?",
    "What budget cap is authorized for SO-1 (id=1.0)?",
    "What budget cap is authorized for SO-1 (id=1.5)?",
    "What budget cap is authorized for SO-1 (id=1e0)?",
    "What budget cap is authorized for SO-1 (budget=1)?",
    "What budget cap is authorized for SO-1 (id=1, amount=600)?",
    "What budget cap is authorized for SO-1 (id=1 650)?",
    "What budget cap is authorized for SO-1 (id=1）?",
    "What budget cap is authorized for SO-1（编号=2）?",
    "What budget cap is authorized for SO-1 (id=1) at 600?",
    "What budget cap is authorized for SO-1 (id=1) or 650?",
    "What budget cap is authorized for sales order ID=11?",
    "What budget cap is authorized for sales order ID=1.0?",
    "What budget cap is authorized for sales order ID=1.5?",
    "What budget cap is authorized for purchase order ID=1?",
    "What budget cap is authorized for (id=1)?",
])
def test_wrong_untyped_decimal_and_extra_numbers_remain_rejected(grader, question):
    assert grader.grade_clarification(case(), question)["verdict"] == "incorrect"


def test_identity_comes_from_actual_order_not_fixed_case_number(grader):
    different = case(27, "SO-42")
    assert grader.grade_clarification(different, "What budget applies to SO-42 (id=27)?")["verdict"] == "correct"
    assert grader.grade_clarification(different, "What budget applies to SO-42 (id=1)?")["verdict"] == "incorrect"
    assert grader.grade_clarification(different, "Please confirm budget 27 for SO-42 (id=27)?")["verdict"] == "incorrect"


def test_candidate_has_a_distinct_host_rubric_revision(grader):
    assert grader.RUBRIC_VERSION == "investigation-business-v1.2"

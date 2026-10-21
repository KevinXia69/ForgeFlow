"""Rules passing does not imply the original overall score passed."""
import copy

import pytest

from forgeflow.api.case_qa import answer_case


def case():
    return {"state": "succeeded", "execution": {"status": "succeeded",
        "writes": [{"document": "PO-1"}, {"document": "MO-1"}],
        "readback": {"records": [{"document": "PO-1"}, {"document": "MO-1"}]},
        "verifier": {"evaluation_scope": "product_original_verifier", "evaluation_valid": True,
            "strict_success": False, "original_passed": False, "status": "failed", "score": 95.18,
            "passed_rule_count": 95, "applicable_rule_count": 95, "failed_rule_count": 0,
            "checks": [{"passed": False, "total": 95, "applicable": 95}]}}}


@pytest.mark.parametrize("question", ["Did execution and verification pass?", "执行和验收结果是什么？"])
def test_explains_score_failure_without_reinterpreting_rule_counts_or_erp_transactions(question):
    value = case()
    before = copy.deepcopy(value)
    reply = answer_case(question, value)
    assert "95.18" in reply["answer"] and "99/100" in reply["answer"]
    assert ("单据条目数不等于 ERP 事务数" if question.startswith("执行") else
            "document entries are not ERP transactions") in reply["answer"]
    assert ("严格验收仍未通过" if question.startswith("执行") else
            "Strict verification did not pass") in reply["answer"]
    assert value == before
    citations = {row["field"]: row["value"] for row in reply["citations"]}
    assert citations["execution.writes"] == 2 and citations["execution.verifier.score"] == 95.18


@pytest.mark.parametrize("patch", [
    {"source": "synthetic_executor"}, {"evaluation_scope": "customer_verifier"},
    {"evaluation_valid": False}, {"evaluation_valid": None}, {"original_passed": None},
    {"passed_rule_count": 94, "failed_rule_count": 1}, {"applicable_rule_count": 0},
    {"score": None}, {"score": float("nan")}, {"failed_rule_count": False},
    {"strict_success": None}, {"applicable_rule_count": None},
    {"applicable_rule_count": 95.5, "passed_rule_count": 95.5},
])
def test_does_not_infer_score_failure_from_unknown_or_different_evidence(patch):
    value = case()
    value["execution"]["verifier"].update(patch)
    assert "99/100" not in answer_case("Did execution and verification pass?", value)["answer"]

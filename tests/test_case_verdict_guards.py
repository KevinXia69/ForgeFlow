"""Model wording cannot replace failed or incomplete execution conclusions."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forgeflow.api.answer_evidence import validate_model_answer
from forgeflow.api.app import _case_model_reply, _case_reply_base


@pytest.fixture
def case():
    return json.loads((Path(__file__).resolve().parents[1] / "web/e2e/fixtures/approved-case.json").read_text())


def unverified(case, variant):
    if variant == "seven_of_eight":
        case["state"] = "failed"
        case["result"]["outcome"] = "verification_failed"
        case["execution"]["verifier"].update(
            status="failed", strict_success=False, original_passed=False, passed_rule_count=7,
            applicable_rule_count=8, failed_rule_count=1, score=87.5,
            checks=[{"kind": "original_verifier", "passed": False, "applicable": 8, "passed_count": 7, "total": 8}],
        )
    elif variant == "contradictory_pass_flag":
        case["execution"]["verifier"]["failed_rule_count"] = 1
    elif variant == "no_verifier":
        case["execution"].pop("verifier")
    elif variant == "no_execution":
        case.update(state="awaiting_approval", execution=None)
    elif variant == "not_started":
        case["execution"] = {"status": "not_started"}
    elif variant == "readback_failed":
        case["execution"]["readback"]["execution_validated"] = False
    elif variant == "readback_missing":
        case["execution"].pop("readback")
    elif variant == "simulation_only":
        case["execution"]["verifier"]["source"] = "synthetic_executor"
    return case


@pytest.mark.parametrize("variant", [
    "seven_of_eight", "contradictory_pass_flag", "no_verifier", "no_execution", "not_started",
    "readback_failed", "readback_missing", "simulation_only",
])
@pytest.mark.parametrize("lang", ["zh", "en"])
def test_unverified_verdict_retains_host_answer_even_if_model_counts_match(case, variant, lang):
    unverified(case, variant)
    question = "原始验收通过、ERP回读成功了吗？" if lang == "zh" else "Did original verification and ERP readback pass?"
    counts = (7, 8) if variant == "seven_of_eight" else (51, 51)
    positive = (f"原始验收已经通过，{counts[1]}项适用规则中通过了{counts[0]}项。ERP回读已经成功。" if lang == "zh" else
                f"Original verification passed, with {counts[0]} of {counts[1]} applicable rules passed. ERP readback was successful.")
    negative = "原始验收未通过；ERP回读尚未确认成功。" if lang == "zh" else "Original verification did not pass; ERP readback is not confirmed."
    before = copy.deepcopy(case)
    fallback = _case_reply_base(case, question, lang)
    for candidate in (positive, negative):
        checked = validate_model_answer(question, case, candidate, lang)
        assert not checked["valid"]
        assert "execution_verdict_requires_deterministic_answer" in checked["issues"]
        reply = _case_model_reply(case, question, lang, candidate, fallback)
        assert reply["source_mode"] == "deterministic"
        assert reply["answer"] == fallback["answer"]
        assert reply["fallback_reason"] == "model_evidence_mismatch"
    assert case == before


@pytest.mark.parametrize("lang", ["zh", "en"])
def test_verified_case_accepts_supported_model_explanation_but_not_reversed_verdict(case, lang):
    question = "原始验收和回读结果如何？" if lang == "zh" else "What were the verification and readback results?"
    correct = "原始验收通过，51项适用规则全部通过。ERP回读成功。" if lang == "zh" else "Original verification passed all 51 applicable rules. ERP readback succeeded."
    wrong_verifier = correct.replace("验收通过", "验收失败") if lang == "zh" else correct.replace("verification passed", "verification failed")
    wrong_readback = correct.replace("回读成功", "回读失败") if lang == "zh" else correct.replace("readback succeeded", "readback failed")
    fallback = _case_reply_base(case, question, lang)
    reply = _case_model_reply(case, question, lang, correct, fallback)
    assert reply["source_mode"] == "model"
    for wrong, issue in ((wrong_verifier, "contradictory_verification_verdict"),
                         (wrong_readback, "contradictory_readback_verdict")):
        assert issue in validate_model_answer(question, case, wrong, lang)["issues"]
        assert _case_model_reply(case, question, lang, wrong, fallback)["source_mode"] == "deterministic"


def test_successful_negative_phrasing_is_not_mistaken_for_a_failed_verdict(case):
    question = "原始验收通过了吗？"
    answer = "原始验收没有失败，51项适用规则全部通过。"
    assert validate_model_answer(question, case, answer)["valid"]


def test_failure_does_not_force_unrelated_cost_answer_to_deterministic(case):
    unverified(case, "seven_of_eight")
    question = "计划成本是多少？"
    candidate = "计划成本4794.94美元；实际成本未知。"
    fallback = _case_reply_base(case, question, "zh")
    assert _case_model_reply(case, question, "zh", candidate, fallback)["source_mode"] == "model"


@pytest.mark.parametrize("question,prefix,verdict", [
    ("目前状态如何？", "案例失败，涉及50台产品。", "原始验收已经通过，8项适用规则中通过7项。"),
    ("计划成本是多少？", "计划成本4794.94美元；实际成本未知。", "ERP回读已经成功。"),
    ("What is the current status?", "The case failed and involves 50 units.",
     "Original verification passed, with 7 of 8 applicable rules passed."),
    ("What is the planned cost?", "Planned cost is 4794.94 USD; actual cost is unknown.",
     "ERP readback was successful."),
])
@pytest.mark.parametrize("variant", ["seven_of_eight", "no_verifier", "readback_failed"])
def test_unsolicited_verdict_in_status_or_cost_answer_cannot_bypass_the_guard(case, question, prefix, verdict, variant):
    unverified(case, variant)
    if variant != "seven_of_eight":
        prefix = prefix.replace("案例失败", "案例已完成").replace("The case failed", "The case succeeded")
    lang = "zh" if "？" in question else "en"
    fallback = _case_reply_base(case, question, lang)
    # An answer confined to the requested facts remains eligible for the model.
    assert validate_model_answer(question, case, prefix, lang)["valid"]
    checked = validate_model_answer(question, case, prefix + verdict, lang)
    assert "execution_verdict_requires_deterministic_answer" in checked["issues"]
    reply = _case_model_reply(case, question, lang, prefix + verdict, fallback)
    assert reply["source_mode"] == "deterministic"
    assert reply["answer"] == fallback["answer"]


@pytest.mark.parametrize("candidate,issue", [
    ("原始验收未全部通过，51项适用规则全部通过。", "contradictory_verification_verdict"),
    ("原始验收没有全部通过，51项适用规则全部通过。", "contradictory_verification_verdict"),
    ("原始验收已经通过51项；但是回读并未成功。", "contradictory_readback_verdict"),
    ("原始验收没有失败，51项全部通过，但回读失败。", "contradictory_readback_verdict"),
    ("Original verification has not passed; 51 of 51 applicable rules passed.", "contradictory_verification_verdict"),
    ("Original verification passed all 51 rules; ERP readback has not succeeded.", "contradictory_readback_verdict"),
])
def test_verified_case_rejects_common_negative_verdict_phrasings(case, candidate, issue):
    assert issue in validate_model_answer("原始验收和回读结果如何？", case, candidate)["issues"]


@pytest.mark.parametrize("candidate", [
    "原始验收没有失败，51项适用规则全部通过。回读成功。",
    "原始验收无失败项，51项适用规则全部通过。回读成功。",
    "Original verification passed all 51 rules, with zero failed rules. Readback succeeded.",
    "Original verification has no failures; 51 of 51 applicable rules passed. Readback did not fail.",
])
def test_successful_no_failure_wording_remains_a_model_answer(case, candidate):
    question = "原始验收和回读结果如何？"
    assert validate_model_answer(question, case, candidate)["valid"]
    fallback = _case_reply_base(case, question, "zh")
    assert _case_model_reply(case, question, "zh", candidate, fallback)["source_mode"] == "model"

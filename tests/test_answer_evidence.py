"""Shared facts answer the observed questions without trusting model wording."""
from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from forgeflow.api.answer_evidence import (
    grounded_answer,
    question_evidence,
    validate_model_answer,
)
from forgeflow.api.case_context import case_context
from forgeflow.api.case_qa import answer_case

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tests/fixtures/case_qa_acceptance.json").read_text())
QUESTIONS = CONTRACT["questions"]


@pytest.fixture
def case():
    return json.loads((ROOT / CONTRACT["source_fixture"]).read_text())


def at(data, field):
    for part in field.split("."):
        data = data[int(part)] if isinstance(data, list) else data[part]
    return data


def coverage(envelope, topic):
    return next(item for item in envelope["coverage"] if item["topic"] == topic)


def test_original_fixture_and_failed_questions_are_frozen():
    source = ROOT / CONTRACT["source_fixture"]
    assert hashlib.sha256(source.read_bytes()).hexdigest() == CONTRACT["source_sha256"]
    assert len(QUESTIONS) == 5
    assert "缺口" in QUESTIONS[0] and "不要用成本差额代替数量" in QUESTIONS[1]


@pytest.mark.parametrize("question", QUESTIONS)
def test_original_questions_get_public_same_case_facts_and_unchanged_records(case, question):
    before = copy.deepcopy(case)
    evidence = question_evidence(case, question)
    reply = grounded_answer(question, case)
    assert case == before
    assert evidence["case_context"]["case_id"] == case["case_id"]
    assert evidence["case_context"]["plan_id"] == case["plan"]["plan_id"]
    assert all(at(case, cite["field"]) == cite["value"] for cite in evidence["citations"])
    assert validate_model_answer(question, case, reply["answer"])["valid"]


def test_large_context_omits_details_but_question_facts_recover_them(case):
    summary = json.loads(case_context(case))
    assert "detailed_actions" in summary["omitted_sections"]
    assert "P00006" not in json.dumps(summary)
    envelope = question_evidence(case, QUESTIONS[0])
    actions = envelope["groups"]["actions"]["rows"]
    assert [(r["kind"], r["quantity"]) for r in actions] == [("cancel_po", 3), ("create_po", 3)]
    assert actions[0]["document"] == "P00006"
    assert actions[0]["supply_role"] == "finished"
    assert actions[0]["uom_label"] == "Units"
    retained = envelope["groups"]["preserved"]["rows"]
    assert [r["document"] for r in retained] == [
        "WH/MO/00001", "WH/MO/00002", "WH/MO/00003", "WH/MO/00004",
        "P00001", "P00002", "P00003", "P00004", "P00005",
    ]
    assert [r["quantity"] for r in retained] == ["15", "6", "3", "16", "15", "16", "22", "29", "47"]
    assert coverage(envelope, "preserved") == {"topic": "preserved", "status": "available", "total": 9, "shown": 9}


def test_numerical_and_document_mutation_changes_facts_not_recorded_fixture(case):
    case["plan"]["actions"][0].update(document="PO-CANCEL-82", quantity=8)
    case["plan"]["actions"][0]["before"]["quantity"] = 8
    case["plan"]["actions"][1].update(document="new:purchase.order:0099", quantity=9)
    case["plan"]["actions"][1]["after"]["quantity"] = 9
    case["plan"]["costs"].update(known_delta="27.50", new_purchase_value="300.50")
    case["plan"]["costs"]["planned"]["total"] = "1000.50"
    case["plan"]["costs"]["baseline"]["total"] = "973.00"
    text = grounded_answer(QUESTIONS[0], case)["answer"]
    assert "PO-CANCEL-82：取消采购，8" in text
    assert "new:purchase.order:0099" in text and "新建采购，9" in text
    cost_text = grounded_answer(QUESTIONS[3], case)["answer"]
    assert "1,000.50" in cost_text and "973.00" in cost_text and "27.50" in cost_text
    assert "4,794.94" not in cost_text and "160.05" not in cost_text


def test_material_units_are_preserved_and_never_collapsed_to_finished_shortfall(case):
    row = case["plan"]["costs"]["lines"][4]
    row["after"].update(quantity="99", uom_label="kg", uom_id=4)
    reply = grounded_answer(QUESTIONS[1], case)
    assert "99 kg" in reply["answer"]
    assert "不同物料" in reply["answer"]
    assert not any(c["field"].endswith("total_quantity") for c in reply["citations"])


def test_absence_empty_and_unexpanded_are_different(case):
    no_actions = copy.deepcopy(case)
    del no_actions["plan"]["actions"]
    assert coverage(question_evidence(no_actions, QUESTIONS[0]), "actions")["status"] == "not_recorded"
    no_actions["plan"]["actions"] = []
    empty = question_evidence(no_actions, QUESTIONS[0])
    assert coverage(empty, "actions")["status"] == "available"
    assert coverage(empty, "actions")["total"] == 0
    limited = question_evidence(case, QUESTIONS[0], max_rows=2)
    assert coverage(limited, "preserved") == {"topic": "preserved", "status": "omitted", "total": 9, "shown": 2, "reason": "row_limit"}
    assert not any("costs.lines.2" in c["field"] for c in limited["citations"])


@pytest.mark.parametrize("budget", [2048, 4096, 8000])
def test_character_budget_keeps_truthful_partial_coverage(case, budget):
    envelope = question_evidence(case, QUESTIONS[0], max_chars=budget)
    assert len(json.dumps(envelope, ensure_ascii=False, sort_keys=True, separators=(",", ":"))) <= budget
    assert any(c["status"] == "omitted" for c in envelope["coverage"])
    for item in envelope["coverage"]:
        rows = envelope["groups"].get(item["topic"], {}).get("rows")
        if rows is not None:
            assert len(rows) == item["shown"]
    assert all(at(case, cite["field"]) == cite["value"] for cite in envelope["citations"])


def test_private_authority_and_unsupported_fields_cannot_enter_facts(case):
    case.update(uid="private-user", owner_uid="private-owner", token="private-token")
    case["plan"]["actions"][0]["before"].update(secret="private-secret", arbitrary_field="private-arbitrary")
    serialized = json.dumps(question_evidence(case, QUESTIONS[0]))
    assert "private-" not in serialized
    assert "token" not in serialized


@pytest.mark.parametrize("pending", ["selection_pending", "revision_pending", "repreparation_pending", "preparation_pending"])
def test_pending_plan_never_exposes_earlier_detail_as_confirmed(case, pending):
    case["decision"][pending] = True
    evidence = question_evidence(case, QUESTIONS[0])
    assert not evidence["groups"]
    assert all(row["status"] == "unavailable" for row in evidence["coverage"])
    assert "P00006" not in grounded_answer(QUESTIONS[0], case)["answer"]


def test_unknown_execution_does_not_create_zero_write_evidence(case):
    case.update(state="execution_unknown", execution={"status": "unknown", "writes": []})
    evidence = question_evidence(case, QUESTIONS[4])
    group = evidence["groups"]["execution"]
    assert "write_count" not in group and "writes" not in group
    assert coverage(evidence, "execution")["status"] == "unavailable"
    reply = grounded_answer(QUESTIONS[4], case)
    assert "可能已经发生写入" in reply["answer"]
    assert "0 条" not in reply["answer"]


def test_selection_conflict_with_prior_receipt_does_not_recursively_read_old_plan(case):
    case["reason"] = "product_selection_conflict"
    reply = answer_case(QUESTIONS[0], case)
    assert "版本冲突" in reply["answer"]
    assert "P00006" not in reply["answer"]


def test_known_zero_cost_is_different_from_unknown_actual_cost(case):
    case["plan"]["costs"]["known_delta"] = "0"
    reply = grounded_answer(QUESTIONS[3], case)
    assert "USD 0.00" in reply["answer"]
    assert "实际财务成本未记录" in reply["answer"]
    assert "取消费用与税费调整等完整费用差额仍未知" in reply["answer"]
    case["plan"]["costs"]["actual_cost"] = "1234.50"
    reply = grounded_answer(QUESTIONS[3], case)
    assert "已记录实际财务成本 USD 1,234.50" in reply["answer"]
    assert "实际财务成本未记录" not in reply["answer"]


def test_model_false_absence_and_mixed_intent_omission_fail(case):
    text = "成本增加了 160.05 美元。记录没有列出单据编号，也没有数量。"
    result = validate_model_answer(QUESTIONS[0], case, text)
    assert not result["valid"]
    assert any(issue.startswith("missing_preserved_summary:") for issue in result["issues"])
    assert "false_absence:actions" in result["issues"]


def test_summary_question_accepts_supported_counts_without_demanding_all_identifiers(case):
    text = "取消3件成品采购，方案替代采购也是3件。保留4张制造单合计40件，以及5张组件采购单。"
    assert validate_model_answer(QUESTIONS[0], case, text)["valid"]
    assert not validate_model_answer(QUESTIONS[1], case, text)["valid"]
    evidence = question_evidence(case, QUESTIONS[0])
    summary = evidence["groups"]["preserved"]["summary"]
    assert summary[0]["document_count"] == 4
    assert summary[0]["quantity_groups"][0]["quantity"] == "40"
    assert summary[1]["document_count"] == 5
    assert len(summary[1]["quantity_groups"]) == 3


def test_cost_answer_only_requires_numbers_actually_requested(case):
    text = "方案规划成本4,794.94美元包括保留及新建的采购与制造安排，比原安排增加160.05美元。实际成本未知，不能称为节省。"
    assert validate_model_answer(QUESTIONS[3], case, text)["valid"]
    assert not validate_model_answer("请列出计划成本、基线、差额和新增采购金额", case, text)["valid"]


def test_model_delivery_overclaim_is_not_accepted(case):
    text = grounded_answer(QUESTIONS[4], case)["answer"]
    assert validate_model_answer(QUESTIONS[4], case, text)["valid"]
    text = text.replace("原始验收只核对", "回读更接近交付证据。原始验收只核对")
    assert "readback_is_not_delivery_evidence" in validate_model_answer(QUESTIONS[4], case, text)["issues"]


def test_contradictory_number_pair_is_not_rescued_by_correct_number_elsewhere(case):
    text = grounded_answer(QUESTIONS[0], case)["answer"]
    text = text.replace("P00006：取消采购，3", "P00006：取消采购，8")
    result = validate_model_answer(QUESTIONS[0], case, text)
    assert "contradictory_quantity:P00006" in result["issues"]


def test_affirmative_delivery_or_savings_not_rescued_by_later_caveat(case):
    text = "客户已经签收。\n" + grounded_answer(QUESTIONS[4], case)["answer"]
    assert "unbound_customer_delivery_claim" in validate_model_answer(QUESTIONS[4], case, text)["issues"]
    text = "这次节省了160.05美元。\n" + grounded_answer(QUESTIONS[3], case)["answer"]
    assert "positive_cost_difference_called_saving" in validate_model_answer(QUESTIONS[3], case, text)["issues"]


def test_explicit_language_and_unrecognized_question_preserve_compatibility(case):
    assert "Existing documents retained" in grounded_answer(QUESTIONS[0], case, "en")["answer"]
    assert "Existing documents retained" in answer_case(QUESTIONS[0], case, "en")["answer"]
    assert validate_model_answer("hello", case, "Hello")["valid"]
    assert validate_model_answer("hello", case, "Hello")["citations"] == []
    with pytest.raises(ValueError):
        answer_case("hello", case, "fr")


def test_validator_handles_numbers_next_to_chinese_text(case):
    text = grounded_answer(QUESTIONS[0], case)["answer"].replace("，3 ", "，3")
    assert validate_model_answer(QUESTIONS[0], case, text)["valid"]


def test_content_fingerprint_changes_on_same_case_revision_data_change(case):
    first = question_evidence(case, QUESTIONS[0])
    case["plan"]["actions"][0]["quantity"] = 77
    second = question_evidence(case, QUESTIONS[0])
    assert first["case_context"] == second["case_context"]
    assert first["snapshot_id"] != second["snapshot_id"]


def test_status_count_is_derived_from_range_and_missing_receipt_is_not_a_success_claim(case):
    evidence = question_evidence(case, QUESTIONS[2])
    assert evidence["groups"]["status"]["order_count"] == 5
    assert "5 张订单" in grounded_answer(QUESTIONS[2], case)["answer"]
    case["order"]["ref"] = "SO-4..10"
    case["order"]["units"] = 70
    case.pop("execution")
    answer = grounded_answer(QUESTIONS[2], case)["answer"]
    assert "7 张订单" in answer and "70 台" in answer
    assert "执行回读尚未确认" in answer
    assert "系统已经执行并回读" not in answer
    assert validate_model_answer(QUESTIONS[2], case, answer)["valid"]
    assert "missing_order_count" in validate_model_answer(QUESTIONS[2], case, "50 张订单，70 台。交付未知。")["issues"]


def test_unknown_order_reference_does_not_create_a_count(case):
    case["order"]["ref"] = "opaque-group-18"
    evidence = question_evidence(case, QUESTIONS[2])
    assert "order_count" not in evidence["groups"]["status"]


def test_status_answer_cannot_skip_state_even_when_numeric_scope_is_correct(case):
    text = "涉及 5 张订单、50 台产品，客户交付未知。"
    assert "missing_case_state" in validate_model_answer(QUESTIONS[2], case, text)["issues"]
    assert validate_model_answer(QUESTIONS[2], case, "记录显示执行已完成。" + text)["valid"]


def test_missing_and_noncomparable_costs_do_not_infer_savings(case):
    case["plan"]["costs"]["comparable"] = False
    answer = grounded_answer(QUESTIONS[3], case)["answer"]
    assert "缺可比基线确认" in answer
    assert "已知规划成本增加" not in answer
    case["plan"].pop("costs")
    case["plan"].pop("new_spend", None)
    answer = grounded_answer(QUESTIONS[3], case)["answer"]
    assert "没有记录可核对的成本金额" in answer
    assert "4,794.94" not in answer


def test_selected_execution_summary_retains_existing_strict_verifier_guards(case):
    case["execution"]["verifier"]["failed_rule_count"] = 1
    answer = grounded_answer(QUESTIONS[4], case)["answer"]
    assert "原始验收结果为失败" in answer
    assert "原始验收结果为通过" not in answer


@pytest.mark.parametrize("question,text,issue", [
    (QUESTIONS[0], "取消3件组件采购，替代采购30件。保留4张制造单和5张采购单。", "contradictory_action_quantity:create"),
    (QUESTIONS[0], "取消3件组件采购，替代采购3件。保留4张制造单和5张采购单。", "contradictory_supply_role:cancel"),
    (QUESTIONS[3], "方案规划成本160.05美元，包括保留采购和制造安排；比原安排增加4794.94美元。实际成本未知，不能称为节省。", "contradictory_cost:planned"),
    (QUESTIONS[3], "方案规划成本4794.94美元，比原安排增加160.05美元，新增采购增量为0。实际成本未知，不能称为节省。", "contradictory_cost:new_purchase_value"),
    (QUESTIONS[2], "案例尚未完成，涉及5张订单、50台产品。实际客户交付未知。", "contradictory_case_state"),
])
def test_named_facts_bind_action_role_cost_label_and_affirmed_state(case, question, text, issue):
    result = validate_model_answer(question, case, text)
    assert not result["valid"]
    assert issue in result["issues"]


def test_range_summary_is_not_treated_as_first_document_quantity(case):
    text = "取消3个单位的成品采购P00006，新采购单new:purchase.order:0006补回同样3个单位。保留4份制造订单WH/MO/00001–00004（共40 Units）以及5份采购单P00001–P00005（DCI-121 31、PDS-116 22、SCS-124 76 Units）。"
    assert validate_model_answer(QUESTIONS[0], case, text)["valid"]


def test_cost_cancellation_fees_do_not_request_cancelled_document_quantities(case):
    question = "请分别说明原安排成本、计划成本、新建采购金额和已知差额；增加还是节省？取消费用和税费是否已经包含？"
    assert question_evidence(case, question)["topics"] == ["costs"]
    assert validate_model_answer(question, case, grounded_answer(question, case)["answer"])["valid"]


@pytest.mark.parametrize("pending", ["selection_pending", "revision_pending", "preparation_pending"])
def test_unrelated_unknown_does_not_validate_unconfirmed_plan_details(case, pending):
    case["decision"][pending] = True
    text = "已确认取消P00006的3件成品采购，并新建3件替代采购，保留4张制造单与5张采购单。交付未知。"
    assert "plan_confirmation_requires_deterministic_answer" in validate_model_answer(QUESTIONS[0], case, text)["issues"]


def test_unknown_execution_forces_existing_deterministic_status_guard(case):
    case.update(state="execution_unknown", execution={"status": "unknown"})
    assert "unknown_execution_requires_deterministic_answer" in validate_model_answer(QUESTIONS[4], case, "已经执行，交付未知。")["issues"]


@pytest.mark.parametrize("question", [QUESTIONS[0], QUESTIONS[3], QUESTIONS[4]])
def test_three_sentence_requests_use_complete_shared_facts_without_extra_planning_metrics(case, question):
    reply = answer_case(question, case)
    assert reply["answer"].count("。") == 3
    assert len(reply["answer"].split("\n\n")) == 3
    assert "策略" not in reply["answer"] and "毛利" not in reply["answer"]
    assert all(at(case, row["field"]) == row["value"] for row in reply["citations"])
    assert validate_model_answer(question, case, reply["answer"])["valid"]


def test_document_detail_request_is_not_compressed_into_a_summary(case):
    reply = answer_case(QUESTIONS[1] + "请用三句话回答。", case)
    for document in ["WH/MO/00001", "WH/MO/00002", "WH/MO/00003", "WH/MO/00004", "P00001", "P00002", "P00003", "P00004", "P00005"]:
        assert document in reply["answer"]
    assert "\n- " in reply["answer"]


def test_normalized_cost_fallback_uses_shared_scalars_not_array_counts(case):
    question = "请分别说明原安排成本、计划成本、新建采购金额和已知差额；取消费用和税费是否已经包含？"
    reply = answer_case(question, case)
    assert reply["source_mode"] == "deterministic"
    assert all(at(case, row["field"]) == row["value"] for row in reply["citations"])
    assert "80" not in reply["answer"] and "策略" not in reply["answer"]


def test_cost_aliases_component_breakdown_and_difference_formula_are_not_another_total(case):
    question = "请分别说明原安排成本、计划成本、新建采购金额和已知差额；增加还是节省？取消费用和税费是否已经包含？"
    text = ("原安排成本：4634.89 美元（原安排规划成本总额）。\n"
            "计划成本：4794.94 美元（方案规划成本总额，其中采购3464.94美元，制造1330.0美元）。\n"
            "新建采购金额：1432.32美元（新建采购单金额）。\n"
            "已知差额：160.05美元，即计划成本减去可比基准（4794.94 − 4634.89 = 160.05）。\n"
            "这是增加，不是节省。实际成本（actual_cost）为空，完整差额也未知，不能断言取消费用或税费已经计入。")
    assert validate_model_answer(question, case, text)["valid"]
    assert not validate_model_answer(question, case, text.replace("计划成本：4794.94", "计划成本：160.05"))["valid"]


def test_cost_question_about_fees_accepts_explicit_unconfirmed_fee_boundary(case):
    question = "原安排成本、计划成本、新建采购金额和已知差额各是多少？取消费用和税费是否包含？"
    text = "原安排成本4634.89美元，计划成本4794.94美元，新建采购金额1432.32美元，已知差额160.05美元。没有单独列示取消费用或税费，无法确认它们是否包含。"
    assert validate_model_answer(question, case, text)["valid"]
    assert not validate_model_answer(question, case, text + "实际成本为0美元。")["valid"]


@pytest.mark.parametrize("countdown,historical", [
    ("距交期还有8天", "按历史快照，距交期还有8天"),
    ("还剩8天", "记录显示当时还剩8天"),
    ("The order is due in 8 days", "As of the historical snapshot, the order was due in 8 days"),
])
def test_relative_deadline_requires_local_historical_qualifier(case, countdown, historical):
    answer = grounded_answer(QUESTIONS[4], case)["answer"]
    issue = "historical_deadline_presented_as_current_countdown"
    assert issue in validate_model_answer(QUESTIONS[4], case, answer + "\n" + countdown + "，但没有收货记录。")["issues"]
    assert issue not in validate_model_answer(QUESTIONS[4], case, answer + "\n" + historical + "。")["issues"]
    assert issue not in validate_model_answer(QUESTIONS[4], case, answer + "\n记录日期为2026-10-24，交期为2026-11-01。")["issues"]

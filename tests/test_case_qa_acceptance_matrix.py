"""Explicit T03/T04/T05/T16 semantics over independent fixture copies."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from forgeflow.api.answer_evidence import grounded_answer, question_evidence

ROOT = Path(__file__).resolve().parents[1]
CONTRACT = json.loads((ROOT / "tests/fixtures/case_qa_acceptance.json").read_text())
SOURCE = ROOT / CONTRACT["source_fixture"]


@pytest.fixture
def record():
    before = SOURCE.read_bytes()
    assert hashlib.sha256(before).hexdigest() == CONTRACT["source_sha256"]
    yield json.loads(before)
    assert SOURCE.read_bytes() == before


def test_t03_each_cost_amount_keeps_its_source_and_unknown_cost_is_not_zero(record):
    question = "请同时列出计划成本、原安排基线、已知成本差额和新增采购金额；实际费用有哪些未知？"
    reply = grounded_answer(question, record)
    citations = {row["field"]: row["value"] for row in reply["citations"]}
    for field, value in {
        "plan.costs.planned.total": "4794.94", "plan.costs.baseline.total": "4634.89",
        "plan.costs.known_delta": "160.05", "plan.costs.new_purchase_value": "1432.32",
    }.items():
        assert citations[field] == value
        assert f"{float(value):,.2f}" in reply["answer"]
    assert "保留" in reply["answer"] and "制造" in reply["answer"]
    assert "实际财务成本未记录" in reply["answer"]
    assert "完整费用差额仍未知" in reply["answer"]
    assert not any("actual_cost" in field and value == 0 for field, value in citations.items())


def test_t04_scope_historical_state_and_delivery_are_independently_evidenced(record):
    question = CONTRACT["questions"][2]
    facts = question_evidence(record, question)
    assert facts["groups"]["status"]["state"] == record["state"]
    assert facts["groups"]["status"]["order_count"] == 5
    assert facts["groups"]["status"]["units"] == 50
    assert next(c for c in facts["coverage"] if c["topic"] == "delivery")["status"] == "not_recorded"
    reply = grounded_answer(question, record)["answer"]
    for expected in ("5 张订单", "50 台", record["updated_at"], "历史记录", "实际交付仍未知"):
        assert expected in reply
    quantity = grounded_answer(CONTRACT["questions"][0], record)["answer"]
    assert "不表示当前仍然缺货" in quantity


def test_t05_execution_is_document_entries_and_scoped_verifier_not_delivery(record):
    question = CONTRACT["questions"][4]
    facts = question_evidence(record, question)
    execution = facts["groups"]["execution"]
    assert execution["write_count"] == len(record["execution"]["writes"]) == 2
    assert execution["passed"] == 51 and execution["applicable"] == 51
    assert execution["evaluation_scope"] == "product_original_verifier"
    assert [row["document"] for row in execution["writes"]] == [row["document"] for row in record["execution"]["writes"]]
    reply = grounded_answer(question, record)["answer"]
    assert all(row["document"] in reply for row in record["execution"]["writes"])
    assert "单据条目数不等于 ERP 事务数" in reply
    assert "只核对该任务的适用业务规则" in reply
    assert re.search(r"ERP 回读(?:不|不能)证明(?:实际)?生产完工、发货或客户签收", reply)


def test_t16_status_order_scope_verifier_and_execution_id_mutation_change_output(record):
    record["state"] = "failed"
    record["result"]["outcome"] = "verification_failed"
    record["order"].update(ref="SO-10..12", units=79, customer="3 confirmed sales orders")
    record["execution"]["verifier"].update(
        status="failed", strict_success=False, original_passed=False, passed_rule_count=7,
        applicable_rule_count=8, failed_rule_count=1, score=87.5,
        checks=[{"kind": "original_verifier", "passed": False, "applicable": 8, "passed_count": 7, "total": 8}],
    )
    record["execution"]["writes"][0]["document"] = "PO-ACTUAL-CHANGED"
    question = "当前状态、订单数、产品数量以及执行回读、验收适用规则是什么？"
    facts = question_evidence(record, question)
    assert facts["groups"]["status"]["state"] == "failed"
    assert facts["groups"]["status"]["order_count"] == 3
    assert facts["groups"]["status"]["units"] == 79
    assert facts["groups"]["execution"]["passed"] == 7
    assert facts["groups"]["execution"]["applicable"] == 8
    reply = grounded_answer(question, record)["answer"]
    assert "3 张订单" in reply and "79 台" in reply and "PO-ACTUAL-CHANGED" in reply
    assert "通过 7 项" in reply and "适用 8 项" in reply
    assert "失败" in reply
    assert "51 项" not in reply and "50 台" not in reply

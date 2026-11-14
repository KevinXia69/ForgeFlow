"""The read-only example is frozen evidence, never current ERP state or authority."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from forgeflow.api.case_qa import _get, answer_case
from forgeflow.api.impact_context import checked_chain
from scripts.build_historical_demo import DEMO_CASE_ID, PLAN_FIELDS, SOURCE, projection

ROOT = Path(__file__).resolve().parents[1]


def demo():
    return projection((ROOT / SOURCE).read_bytes())


def test_published_example_is_exact_bound_projection_without_approval_or_audit():
    expected = demo()
    original = json.loads((ROOT / SOURCE).read_text())
    case = expected["case"]
    assert case["synthetic"] is True and case["case_id"] == DEMO_CASE_ID
    assert case["approval"] is None and case["execution"] is None
    assert not {"audit", "history", "decision", "owner_uid", "result", "comparison"}.intersection(case)
    assert case["plan"] == {key: original["plan"][key] for key in PLAN_FIELDS}
    assert checked_chain(case["plan"]) is not None
    for bundle in ("web", "web-public"):
        assert json.loads((ROOT / bundle / "src/historicalDemo.json").read_text()) == expected
        assert DEMO_CASE_ID in (ROOT / bundle / "src/historicalDemoRoute.ts").read_text()


def test_source_changes_cannot_silently_refresh_demo_from_current_data():
    changed = json.loads((ROOT / SOURCE).read_text())
    changed["order"]["units"] = 999
    with pytest.raises(ValueError, match="historical_demo_source_changed"):
        projection(json.dumps(changed).encode())


@pytest.mark.parametrize("question", ["影响范围是什么？", "What is the impact scope?"])
def test_scope_is_compact_and_only_expands_directly_linked_order(question):
    case = demo()["case"]
    before = copy.deepcopy(case)
    reply = answer_case(question, case)
    assert reply["answer_basis"] == "case_record"
    assert "S00004" in reply["answer"] and "S00001" not in reply["answer"]
    assert "13" in reply["answer"] and "7" in reply["answer"] and "3" in reply["answer"]
    assert "50" not in reply["answer"] and "WH/MO" not in reply["answer"]
    assert len(reply["answer"]) < 1100 and len(reply["citations"]) < 50
    assert "not proof of receipt" in reply["answer"] or "不代表已收货" in reply["answer"]
    assert all(_get(case, row["field"]) == row["value"] for row in reply["citations"])
    assert case == before


@pytest.mark.parametrize("question", ["S00004 的供应分配是什么？", "S00004 的供应关系是什么？", "Explain the supply allocation for S00004"])
def test_relationship_wording_still_opens_the_detailed_bound_path(question):
    reply = answer_case(question, demo()["case"])
    assert "S00004" in reply["answer"] and "S00001" not in reply["answer"]
    assert "WH/MO/00004" in reply["answer"]
    assert any("impact_chain.edges" in row["field"] for row in reply["citations"])


@pytest.mark.parametrize("fault", ["missing", "other_plan", "changed_source"])
def test_scope_refuses_missing_or_rebound_graph(fault):
    case = demo()["case"]
    if fault == "missing":
        case["plan"]["impact_chain"] = None
    elif fault == "other_plan":
        case["plan"]["plan_id"] = "0" * 64
    else:
        case["plan"]["impact_chain"]["source_facts"]["orders"][0]["quantity"] = 999
    reply = answer_case("影响范围是什么？", case)
    assert "S00004" not in reply["answer"]
    assert "未知" in reply["answer"] or "尚未记录" in reply["answer"]
    assert not any("impact_chain" in row["field"] for row in reply["citations"])


def test_unknown_order_and_pending_selection_do_not_use_historical_answer():
    case = demo()["case"]
    reply = answer_case("SO00004 的供应分配是什么？", case)
    assert "不能拿其他订单代替" in reply["answer"] and "S00004" not in reply["answer"]
    case["decision"] = {"selection_pending": True}
    reply = answer_case("影响范围是什么？", case)
    assert "确认尚未收到" in reply["answer"] and "13" not in reply["answer"]


def test_named_document_keeps_its_own_scope_instead_of_default_disruption():
    reply = answer_case("P00001 的影响范围是什么？", demo()["case"])
    assert "S00001" in reply["answer"] and "S00004" not in reply["answer"]


@pytest.mark.parametrize("question", [
    "SO00004 的影响范围是什么？", "What is the impact on S99999?",
    "S00004 和 S99999 的影响范围是什么？", "What is the impact on S00004 and SO00004?",
])
def test_unknown_or_mixed_order_scope_does_not_substitute_known_disruption(question):
    reply = answer_case(question, demo()["case"])
    assert "S00004" not in reply["answer"] and "13" not in reply["answer"]
    assert "不能拿其他订单代替" in reply["answer"] or "cannot substitute" in reply["answer"]
    assert not any("impact_chain" in row["field"] for row in reply["citations"])


@pytest.mark.parametrize("question", ["S00001 的影响范围是什么？", "What is the impact on S00001?"])
def test_known_non_direct_order_stays_within_explicit_scope(question):
    reply = answer_case(question, demo()["case"])
    assert "S00001" in reply["answer"] and "S00004" not in reply["answer"]
    assert "关系未记录" in reply["answer"] or "relationship not recorded" in reply["answer"]

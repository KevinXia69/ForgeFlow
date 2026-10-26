"""Exclusions are clause-local and the evidence fallback cannot redispatch."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from forgeflow.api import case_qa
from forgeflow.api.answer_evidence import grounded_answer, requested_topics


@pytest.fixture
def case():
    return json.loads((Path(__file__).resolve().parents[1] / "web/e2e/fixtures/approved-case.json").read_text())


@pytest.mark.parametrize("question", [
    "不要提成本，请列出保留单据。",
    "请列出保留单据，不要提成本。",
    "不要用成本差额代替数量；请列出保留单据。",
    "不要提成本。但是请列出保留单据。",
    "Don't mention costs; list retained documents.",
    "List retained documents; don't mention costs.",
    "Do not use cost differences instead of quantities, list retained documents.",
    "Do not mention costs but list retained documents.",
    "Without discussing costs, list retained documents.",
    "Without mentioning costs; list retained documents.",
])
def test_exclusion_order_does_not_hide_the_requested_document_facts(case, question):
    assert requested_topics(question) == ["preserved"]
    for answer in (case_qa.answer_case(question, case), grounded_answer(question, case)):
        assert "P00001" in answer["answer"] and "WH/MO/00004" in answer["answer"]
        assert "4,794.94" not in answer["answer"]
        assert {item["topic"] for item in answer["evidence"]["coverage"]} == {"preserved"}


@pytest.mark.parametrize("question", ["不要列出保留单据。", "Don't list retained documents.", "hello"])
def test_grounded_no_topic_fallback_cannot_reenter_dispatch(case, question, monkeypatch):
    assert requested_topics(question) == []

    def forbid_dispatch(*args, **kwargs):
        raise AssertionError("a grounded fallback must not redispatch")

    monkeypatch.setattr(case_qa, "_answer_case_impl", forbid_dispatch)
    reply = grounded_answer(question, case)
    assert reply["answer"].strip()
    assert reply["source_mode"] == "deterministic"
    assert reply["evidence"]["topics"] == []


@pytest.mark.parametrize("question", ["不要提成本，请列出保留单据。", "Don't mention costs; list retained documents."])
def test_pending_plan_guard_survives_exclusion_routing(case, question):
    case["decision"]["selection_pending"] = True
    reply = case_qa.answer_case(question, case)
    assert "P00001" not in reply["answer"]
    assert all(item["status"] == "unavailable" for item in reply["evidence"]["coverage"])

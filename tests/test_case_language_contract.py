"""Ten first-time-reader questions must open with plain case context."""
from __future__ import annotations

import re

import pytest
from test_case_impact_qa import impact_case

from forgeflow.api.case_qa import answer_case

QUESTIONS = [
    "What happened to this order?",
    "Which orders are affected?",
    "What do I need to decide?",
    "What will the plan change?",
    "What is not known yet?",
    "Has this been executed and verified?",
    "Why do I need to approve it?",
    "What does readback mean here?",
    "这单出了什么事？",
    "我现在需要决定什么？",
]
BANNED = ("firstly", "moreover", "in conclusion", "it is worth noting", "as an ai",
          "首先", "其次", "综上所述", "值得注意的是", "作为一个 ai")


@pytest.mark.parametrize("question", QUESTIONS)
def test_first_paragraph_is_plain_case_context(question: str) -> None:
    answer = answer_case(question, impact_case().view())["answer"]
    lead = answer.split("\n\n", 1)[0]
    assert "Clean Room Sealed Fixture" in lead or "案例" in lead or "case" in lead.lower()
    assert not re.search(r"[a-f0-9]{16,}", lead, re.I)
    assert not any(phrase in lead.lower() for phrase in BANNED)
    assert len(lead) <= 420


def test_server_strips_full_case_reference() -> None:
    from forgeflow.api.case_qa import strip_case_reference

    case_id = "a" * 32
    text = f"The plan is applied.\n\nReference: Case {case_id}\n"
    assert strip_case_reference(text, case_id, "en") == "The plan is applied."
    assert case_id not in strip_case_reference(f"Answer {case_id}", case_id, "zh")

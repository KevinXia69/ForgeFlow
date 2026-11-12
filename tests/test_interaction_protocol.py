"""Independent, prewritten M4 protocol assertions; no production parser gold."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
spec = importlib.util.spec_from_file_location("interaction_protocol", ROOT / "scripts/interaction_protocol.py")
protocol = importlib.util.module_from_spec(spec)
spec.loader.exec_module(protocol)
SUITE = json.loads((ROOT / "data/interaction-semantics/protocol-v1.json").read_text())


@pytest.mark.parametrize("row", SUITE["assertions"], ids=lambda row: row["assertion_id"])
def test_twenty_four_independent_protocol_assertions(row):
    actual = protocol.proposal_audit(row["observation"], row["gold"], row["ticket"], row["currency"])
    assert actual["status"] == row["expected_protocol_status"], actual


def test_exactly_twelve_declared_minimal_pairs():
    assert len(SUITE["assertions"]) == 24
    families = {row["family"] for row in SUITE["assertions"]}
    assert len(families) == 12
    assert all(sum(row["family"] == family for row in SUITE["assertions"]) == 2 for family in families)


def test_correct_enum_wrong_language_never_earns_model_credit():
    row = copy.deepcopy(SUITE["assertions"][7])
    accepted = copy.deepcopy(row["observation"])
    audit = protocol.observe(row["observation"], accepted,
        "What budget cap in USD do you authorize for SO-317?", row["gold"], row["ticket"], "USD")
    assert audit["model"]["status"] == "observed_failure"
    assert audit["renderer_repaired_language"] is True
    assert audit["host"]["status"] == "success"


def test_any_followup_does_not_receive_another_budget_after_decision():
    node = {"allowed_actions": [{"kind": "budget_authorization_proposal", "order_id": "317", "currency": "USD", "amount": "1420.00"}],
            "then": {"confirm": True}}
    result = protocol.advance(node, {"kind": "ask_followup", "need": "current_authorization"},
        "What budget cap do you authorize for SO-317?", {"order_id": "317", "order_name": "SO-317"}, "USD")
    assert result["status"] == "observed_failure" and result["transition"] is None
    assert result["audit"]["action"]["reason"] == "unnecessary_question"


def test_unknown_wording_remains_unjudged_not_accepted():
    result = protocol.question_audit("A little more context would help.", {"order_id": "317", "order_name": "SO-317"}, "USD")
    assert result["status"] == "unknown"


def test_optional_model_text_is_separate_from_required_displayed_question():
    row = SUITE["assertions"][0]
    action = {"kind": "ask_followup", "need": "current_authorization"}
    result = protocol.observe(action, action, None, row["gold"], row["ticket"], "USD")
    assert result["model"]["status"] == "success"
    assert result["model"]["language"]["status"] == "not_reached"
    assert result["host"]["status"] == "unknown" and result["advance"] is False


def test_source_ranges_use_codepoints_and_reject_hidden_history():
    text = "🧾 当前预算未授权"
    proposal = {"schema_version": 2, "kind": "ask_followup", "need": "current_authorization",
                "source_spans": [{"reply_id": "current", "start": 0, "end": len(text)}]}
    visible = [{"reply_id": "current", "text": text}]
    assert protocol.source_contract_audit(proposal, visible, "current")["status"] == "success"
    proposal["source_spans"][0]["end"] += 1
    assert protocol.source_contract_audit(proposal, visible, "current")["status"] == "observed_failure"
    proposal["source_spans"][0]["reply_id"] = ["untrusted-list"]
    assert protocol.source_contract_audit(proposal, visible, "current")["status"] == "observed_failure"


def test_host_fallback_advances_only_explicit_safe_branch_and_never_earns_model_credit():
    row = SUITE["assertions"][0]
    node = {**row["gold"], "allow_host_fallback": True, "then": {"reply": "A prewritten response"}}
    fallback = {"accepted": False, "reason": "no_accepted_model_proposal", "host_question": "What budget cap do you authorize for SO-317?"}
    result = protocol.advance_fallback(node, fallback, row["ticket"], "USD")
    assert result["status"] == "success" and result["audit"]["model_credit"] is False
    node["allowed_actions"] = [{"kind": "budget_authorization_proposal", "order_id": "317", "amount": "1420.00", "currency": "USD"}]
    assert protocol.advance_fallback(node, fallback, row["ticket"], "USD")["transition"] is None


def test_planned_denominator_and_not_reached_are_preserved():
    result = protocol.aggregate([{"attempt_id": "a", "status": "observed_failure", "stages": {
        "parent": {"status": "observed_failure"}, "child": {"status": "not_reached"}}}], ["a", "b"])
    assert result["planned_denominator"] == 2 and result["dispatched_denominator"] == 1
    assert result["outcomes"]["not_started"] == 1
    assert result["stages"]["child"]["not_reached"] == 1
    assert result["stages"]["child"]["not_started"] == 1


def test_protocol_does_not_import_production_interpreter_or_admission():
    source = (ROOT / "scripts/interaction_protocol.py").read_text()
    assert "from forgeflow" not in source and "import forgeflow" not in source


@pytest.mark.parametrize("suffix,status", [
    ("Approve the plan.", "observed_failure"),
    ("Lower the margin.", "observed_failure"),
    ("Apply the plan.", "observed_failure"),
    ("Do not approve the plan.", "success"),
    ("Please do not lower the margin.", "success"),
    ("We cannot execute the plan.", "success"),
    ("I will approve the plan.", "unknown"),
])
def test_extra_sentences_are_audited_separately_from_correct_budget_question(suffix, status):
    text = "What budget cap in USD do you authorize for SO-317? " + suffix
    assert protocol.question_audit(text, {"order_id": "317", "order_name": "SO-317"}, "USD")["status"] == status

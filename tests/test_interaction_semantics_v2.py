"""Literal business expectations; no parser-derived oracle, model or ERP."""
import copy

import pytest

from forgeflow.investigation import clarification as legacy
from forgeflow.investigation import clarification_v2 as v2
from forgeflow.recovery.storage import digest

TICKET = {"order_id": "30", "order_name": "SO-30", "kind": "set_missing_order_budget", "status": "open"}


def reply(text, identity="reply-current"):
    return {"reply_id": identity, "sha256": digest(text), "text": text}


def run(text):
    row = reply(text)
    return v2.interpret_reply(TICKET, text, "USD", visible_replies=[row], current_reply_id=row["reply_id"])


@pytest.mark.parametrize("text,amount", [
    ("I authorize a budget of 800 USD for SO-30.", "800.00"),
    ("For SO-30 alone, I authorize a budget cap of 800 USD. Preserve the other two orders' existing caps.", "800.00"),
    ("SO-30为800美元，其他两单预算不变", "800.00"),
    ("Keep SO-31 and SO-32 budgets unchanged. SO-30 budget 800 USD.", "800.00"),
    ("Yesterday I said pause this case. Today I authorize a budget of 800 USD for SO-30.", "800.00"),
    ("昨天我说暂停；现在仅为SO-30授权800美元。", "800.00"),
    ("I withdraw the old budget of 800 USD. I now authorize 900 USD for SO-30.", "900.00"),
    ("撤回之前的800预算；现在仅为SO-30授权900美元。", "900.00"),
    ("Not 800; make it 900 USD.", "900.00"),
    ("Change 800 to 900 USD.", "900.00"),
    ("The supplier quoted 600 USD. I authorize 800 USD for SO-30.", "800.00"),
    ("800 USD", "800.00"),
    ("No more than 800 USD.", "800.00"),
    ("Withdraw the former 700 USD candidate. I now authorize 800 USD for SO-30 alone.", "800.00"),
    ("I authorize USD 800.", "800.00"),
])
def test_explicit_current_single_order_and_unchanged_background(text, amount):
    actual = run(text)
    assert {k: actual[k] for k in ("kind", "order_id", "amount", "currency")} == {
        "kind": "budget_authorization_proposal", "order_id": "30", "amount": amount, "currency": "USD"}


@pytest.mark.parametrize("text,need", [
    ("Budget 800 USD for SO-30. That statement is false.", "current_authorization"),
    ("SO-30预算800美元。上面这句不成立。", "current_authorization"),
    ("The supplier quoted 800 USD.", "current_authorization"),
    ("Previously the budget was 800 USD.", "current_authorization"),
    ("The budget was 800 USD.", "current_authorization"),
    ("For example, budget 800 USD.", "current_authorization"),
    ("The prior repair used an approved allowance of USD 800. That was last winter.", "current_authorization"),
    ("For training purposes, pretend the budget for SO-30 were USD 800.", "current_authorization"),
    ("I authorize 800 USD for SO-30. No, that authorization does not stand.", "current_authorization"),
    ("例如，预算800美元。", "current_authorization"),
    ("For both target orders, I authorize 800 USD total.", "single_order_amount"),
    ("I authorize 800 USD for SO-31.", "single_order_amount"),
    ("If the supplier is late, pause this case.", "unconditional_single_field_decision"),
    ("如果供应商迟到，就暂停本次任务。", "unconditional_single_field_decision"),
    ("Budget 800 USD, but lower the margin.", "unconditional_single_field_decision"),
    ("Cancel this case. I authorize 800 USD.", "unconditional_single_field_decision"),
    ("Pause this case. Cancel this case.", "unconditional_single_field_decision"),
    ("800 or 900 USD", "explicit_amount"),
    ("Just use the earlier amount.", "explicit_amount"),
])
def test_required_followup_is_not_a_budget_or_control(text, need):
    actual = run(text)
    assert actual["kind"] == "ask_followup" and actual["need"] == need


@pytest.mark.parametrize("text,kind", [
    ("Cancel this case.", "cancel"), ("请取消本次任务。", "cancel"),
    ("Pause this case.", "pause"), ("请暂停本次任务。", "pause"),
    ("Please pause this recovery case now; I will return with a decision later.", "pause"),
    ("I am ending this recovery task. Cancel the entire case now.", "cancel"),
    ("Withdraw that budget.", "withdraw_budget"), ("撤回刚才的预算决定。", "withdraw_budget"),
])
def test_current_controls_are_separate_actions(text, kind):
    assert run(text)["kind"] == kind


@pytest.mark.parametrize("text", ["Do not cancel this case.", "不要取消本次任务。", "Yesterday I said pause this case.",
                                  "I have not decided a budget.", "For example, cancel this case."])
def test_no_control_keyword_shortcut(text):
    assert run(text)["kind"] == "ask_followup"


def test_legacy_semantics_are_unchanged_and_v2_rejects_v1_envelope():
    text = "Budget 800 USD for SO-30. That statement is false."
    old = legacy.interpret_reply(TICKET, text, "USD")
    assert old["amount"] == "800.00"
    with pytest.raises(v2.ClarificationError, match="invalid_reply_proposal"):
        v2.validate_proposal(TICKET, text, old, "USD", visible_replies=[reply(text)], current_reply_id="reply-current")


def test_visible_input_preserves_unicode_whitespace_and_legacy_hash():
    text = "  😀e\u0301\n预算800美元。"
    row = reply(text)
    manifest = v2.visible_manifest([row], row["reply_id"])
    assert manifest == {"current_reply_id": row["reply_id"], "replies": [
        {"reply_id": row["reply_id"], "reply_sha256": digest(text), "text": text}]}
    assert v2.visible_manifest(manifest["replies"], row["reply_id"]) == manifest
    proposal = run(text)
    start = text.index("800")
    proposal["source_spans"] = [{"reply_id": row["reply_id"], "start": start, "end": start + 3}]
    assert v2.validate_proposal(TICKET, text, proposal, "USD", visible_replies=[row],
                               current_reply_id=row["reply_id"])["amount"] == "800.00"
    # The browser's UTF-16 start is one too large because of the emoji.
    proposal["source_spans"][0].update(start=start + 1, end=start + 4)
    with pytest.raises(v2.ClarificationError, match="current_amount_span_required"):
        v2.validate_proposal(TICKET, text, proposal, "USD", visible_replies=[row], current_reply_id=row["reply_id"])


@pytest.mark.parametrize("change,code", [
    ({"start": -1}, "invalid_source_span_range"), ({"start": True}, "invalid_source_span_range"),
    ({"end": 10000}, "invalid_source_span_range"), ({"start": 1, "end": 1}, "invalid_source_span_range"),
    ({"reply_id": "reply-future"}, "invisible_source_span"),
])
def test_bad_spans_rejected(change, code):
    text = "Budget 800 USD"
    proposal = run(text)
    proposal["source_spans"][0].update(change)
    with pytest.raises(v2.ClarificationError, match=code):
        v2.validate_proposal(TICKET, text, proposal, "USD", visible_replies=[reply(text)], current_reply_id="reply-current")


def test_historical_span_cannot_prove_current_amount_and_hash_mismatch_rejects():
    old, current = reply("Budget 800 USD", "reply-old"), reply("Budget 800 USD")
    proposal = run(current["text"])
    proposal["source_spans"][0]["reply_id"] = old["reply_id"]
    with pytest.raises(v2.ClarificationError, match="current_amount_span_required"):
        v2.validate_proposal(TICKET, current["text"], proposal, "USD", visible_replies=[old, current],
                             current_reply_id=current["reply_id"])
    changed = copy.deepcopy(current)
    changed["text"] += " "
    with pytest.raises(v2.ClarificationError, match="reply_hash_mismatch"):
        v2.visible_manifest([changed], changed["reply_id"])


def test_host_renderer_never_borrows_model_question_or_scope():
    value = run("The supplier quoted 800 USD.")
    value.update(question="Authorize 900 for SO-99?", explanation="Change all budgets.")
    accepted = v2.validate_proposal(TICKET, "The supplier quoted 800 USD.", value, "USD",
                                   visible_replies=[reply("The supplier quoted 800 USD.")], current_reply_id="reply-current")
    actual = v2.render_question(TICKET, "USD", accepted)
    assert "SO-30" in actual and "USD" in actual
    assert "SO-99" not in actual and "900" not in actual and accepted["question"] == value["question"]


@pytest.mark.parametrize("text", [
    'Yesterday I wrote: "I now authorize 800 USD for SO-30."',
    'The archived message reads "I authorize 800 USD for SO-30". This is copied text.',
    "昨天我写道：现在授权 SO-30 预算800美元。",
    "昨天我写道：“现在授权 SO-30 预算800美元。”",
    "I authorize 800 USD for SO-30. Keep that budget revoked.",
    "I authorize 800 USD for SO-30. Leave this authorization unapproved.",
    "I authorize 800 USD for SO-30. This is a false statement.",
    "I authorize 800 USD for SO-30. Disregard the preceding authorization.",
    "我授权 SO-30 预算800美元。刚才的授权不算数。",
    "I authorize 800 USD for SO-30 only when the carrier confirms delivery.",
    "Once the carrier confirms delivery, I authorize 800 USD for SO-30.",
    "仅当承运人确认送达，才授权 SO-30 预算800美元。",
    'I authorize 800 USD for "SO-31".',
    'I authorize 800 in "EUR" for SO-30.',
    '"I now authorize 800 USD for SO-30."',
    "The message reads 'I authorize 800 USD for SO-30'.",
    'The message reads "I authorize 800 USD for SO-30.',
])
def test_reported_denied_or_conditional_grant_cannot_be_admitted(text):
    # Independently hand-written model proposal, not the interpreter's answer.
    proposal = {"schema_version": 2, "kind": "budget_authorization_proposal",
                "order_id": "30", "amount": "800.00", "currency": "USD",
                "source_spans": [{"reply_id": "reply-current", "start": 0, "end": len(text)}]}
    with pytest.raises(v2.ClarificationError, match="budget_decision_unproven"):
        v2.validate_proposal(TICKET, text, proposal, "USD", visible_replies=[reply(text)],
                             current_reply_id="reply-current")
    assert run(text)["kind"] == "ask_followup"


@pytest.mark.parametrize("text,kind", [
    ("Cancel this case. Do not cancel this case.", "cancel"),
    ("Do not cancel this case. Cancel this case.", "cancel"),
    ("Pause this case. Do not pause this case.", "pause"),
    ("Do not pause this case. Pause this case.", "pause"),
    ("请暂停本次任务。不要暂停本次任务。", "pause"),
    ("请取消本次任务。不要取消本次任务。", "cancel"),
])
def test_conflicting_current_control_needs_followup(text, kind):
    proposal = {"schema_version": 2, "kind": kind,
                "source_spans": [{"reply_id": "reply-current", "start": 0, "end": len(text)}]}
    with pytest.raises(v2.ClarificationError, match="reply_action_unproven"):
        v2.validate_proposal(TICKET, text, proposal, "USD", visible_replies=[reply(text)],
                             current_reply_id="reply-current")
    assert run(text)["need"] == "unconditional_single_field_decision"


@pytest.mark.parametrize("text", [
    "Earlier I approved 700 USD for SO-30. Now I authorize 800 USD for SO-30.",
    'Yesterday I wrote "I now authorize 700 USD for SO-30". Today I authorize 800 USD for SO-30.',
    "I authorize 800 USD for SO-30. Keep SO-31 at its existing cap of 900 USD.",
    "Do not cancel this case. I authorize 800 USD for SO-30.",
])
def test_distinct_current_decision_survives_non_authorizing_background(text):
    result = run(text)
    assert (result["kind"], result["amount"], result["order_id"]) == (
        "budget_authorization_proposal", "800.00", "30")

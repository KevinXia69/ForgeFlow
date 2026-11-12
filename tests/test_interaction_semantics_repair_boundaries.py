"""Independent literal boundary controls for the next interaction repair.

These expected intents are written before examining repair outputs. Handwritten
proposals exercise host admission independently of the rule method's answer.
No model, ERP, historical score repair, or externally visible action is used.
"""
import copy

import pytest

from forgeflow.investigation import clarification_v2 as v2
from forgeflow.recovery.storage import digest

TICKET = {"order_id": "30", "order_name": "SO-30", "kind": "set_missing_order_budget", "status": "open"}


def rows(text, history=()):
    return [{"reply_id": f"old-{i}", "text": value, "sha256": digest(value)}
            for i, value in enumerate(history)] + [{"reply_id": "current", "text": text, "sha256": digest(text)}]


def proposed(text, kind, amount="800.00"):
    value = {"schema_version": 2, "kind": kind,
             "source_spans": [{"reply_id": "current", "start": 0, "end": len(text)}]}
    if kind == "budget_authorization_proposal":
        value.update(order_id="30", currency="USD", amount=amount)
    return value


def admit(text, proposal, history=()):
    return v2.validate_proposal(TICKET, text, proposal, "USD", visible_replies=rows(text, history),
                                current_reply_id="current")


def interpret(text, history=()):
    return v2.interpret_reply(TICKET, text, "USD", visible_replies=rows(text, history), current_reply_id="current")


@pytest.mark.parametrize("text", [
    "My answer now is 800 USD.",
    "My new answer for SO-30 is 800 USD.",
    "SO-31 already has its own approved budget. I authorize 800 USD for SO-30; leave SO-31 unchanged.",
    "SO-31 already has an approved budget. For SO-30 only I authorize USD 800; keep the other order unchanged.",
    "For example, the old answer was 700 USD. My answer now is 800 USD.",
    "Yesterday I wrote a budget of 700 USD. My answer now is 800 USD.",
])
def test_current_answer_and_explicit_unchanged_background_admit_literal_budget(text):
    expected = proposed(text, "budget_authorization_proposal")
    actual = admit(text, expected)
    assert {key: actual[key] for key in ("kind", "order_id", "amount", "currency")} == {
        "kind": "budget_authorization_proposal", "order_id": "30", "amount": "800.00", "currency": "USD"}
    assert interpret(text)["amount"] == "800.00"


def test_visible_history_supplies_no_old_amount_and_cannot_veto_a_new_current_answer():
    history = ["I authorize 700 USD for SO-30.", "Withdraw my pending budget candidate. Keep the case open."]
    current = "My answer now is 800 USD."
    assert admit(current, proposed(current, "budget_authorization_proposal"), history)["amount"] == "800.00"
    assert interpret(current, history)["amount"] == "800.00"
    with pytest.raises(v2.ClarificationError):
        admit(current, proposed(current, "budget_authorization_proposal", "700.00"), history)
    for no_number in ("My answer now is the previous amount.", "Use my former answer."):
        with pytest.raises(v2.ClarificationError):
            admit(no_number, proposed(no_number, "budget_authorization_proposal", "700.00"), history)
        assert interpret(no_number, history)["kind"] == "ask_followup"


@pytest.mark.parametrize("text", [
    "My answer was 800 USD.",
    "Yesterday my answer was 800 USD.",
    "Yesterday I wrote, my answer now is 800 USD.",
    "Yesterday I wrote, I now authorize 800 USD for SO-30.",
    "For example, my answer now is 800 USD.",
    "For example, I now authorize 800 USD for SO-30.",
    "For training purposes, my answer now is 800 USD.",
    'Yesterday I wrote "My answer now is 800 USD."',
    'The message reads "My answer now is 800 USD."',
    'My answer now is "800 USD".',
    "Would my answer of 800 USD be sufficient?",
    "My answer now would be 800 USD.",
    "My answer now is 800 USD, if the delivery is on time.",
    "My answer now is 800 USD only when the carrier confirms.",
    "My answer now is 800 USD. That statement is false.",
    "My answer now is 800 USD. I do not authorize that amount.",
    "My answer now is 800 USD. Keep that budget revoked.",
    "My new answer for SO-31 is 800 USD.",
    "My new answer for SO-30 is 800 EUR.",
    "My answer now is 800 USD; lower the margin too.",
    "My answer now is 800 USD for both orders.",
    "My answer now is 800 or 900 USD.",
    "The supplier says my answer now is 800 USD.",
])
def test_noncurrent_uncertain_denied_or_outside_answer_cannot_admit_budget(text):
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "budget_authorization_proposal"))
    assert interpret(text)["kind"] != "budget_authorization_proposal"


@pytest.mark.parametrize("text", [
    "Withdraw my pending budget candidate. Keep the case open.",
    "Remove the budget candidate from my previous reply; keep investigating the same case.",
    "Remove any pending budget candidate. I have no approved amount for SO-30.",
    "Withdraw this budget candidate. Cancel it entirely.",
])
def test_current_budget_withdrawal_is_admitted_but_never_case_cancel(text):
    assert admit(text, proposed(text, "withdraw_budget"))["kind"] == "withdraw_budget"
    assert interpret(text)["kind"] == "withdraw_budget"
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "cancel"))


@pytest.mark.parametrize("text", [
    "Do not withdraw my pending budget candidate.",
    "Never withdraw this budget candidate.",
    "I do not revoke the budget.",
    "Please do not remove the budget candidate.",
    "I have not withdrawn my budget candidate.",
    "If delivery slips, withdraw the pending budget candidate.",
    "Only when delivery slips, remove this budget candidate.",
    "Yesterday I withdrew the budget candidate.",
    'For example, "Withdraw the pending budget candidate."',
    "Withdraw the budget candidate for SO-31.",
    "Withdraw my pending budget candidate. Do not withdraw it.",
])
def test_negated_conditional_historical_or_wrong_order_withdrawal_cannot_be_admitted(text):
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "withdraw_budget"))
    assert interpret(text)["kind"] != "withdraw_budget"
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "cancel"))


@pytest.mark.parametrize("text", [
    "End this recovery case now. Cancel it entirely.",
    "Please end this recovery case now.",
    "Cancel this recovery case now. End it entirely.",
])
def test_current_whole_case_end_is_cancel_not_budget_withdrawal(text):
    assert admit(text, proposed(text, "cancel"))["kind"] == "cancel"
    assert interpret(text)["kind"] == "cancel"
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "withdraw_budget"))


@pytest.mark.parametrize("text", [
    "Cancel it entirely.",
    "End it entirely.",
    "Do not end this recovery case.",
    "Do not cancel this recovery case. End it entirely.",
    "End this recovery case now. Do not cancel it.",
    "End this recovery case now. Do not end it.",
    "If delivery slips, end this recovery case. Cancel it entirely then.",
    "End this recovery case only when the carrier confirms.",
    "Yesterday I ended this recovery case.",
    'Yesterday I wrote "End this recovery case now. Cancel it entirely."',
    "For example, end this recovery case now.",
    "End this recovery case now. Pause this case now.",
    "End this recovery case now. I authorize 800 USD for SO-30.",
    "End this budget candidate now. Cancel it entirely.",
    "End the case for SO-31 now.",
])
def test_ambiguous_or_noncurrent_end_cannot_cancel_the_current_case(text):
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "cancel"))
    assert interpret(text)["kind"] != "cancel"


@pytest.mark.parametrize("text", [
    "SO-31 already has its own approved budget. I authorize 800 USD for SO-30; increase SO-31 to 900 USD.",
    "SO-31 already has its own approved budget. I authorize 800 USD for SO-30; change its cap to 900 USD.",
    "SO-31 already has its own approved budget. I authorize 800 USD for SO-30 and SO-31.",
    "Keep SO-31 at USD 900. I authorize 800 USD for SO-30.",
    "Keep SO-31 at 900 USD. I authorize 800 USD for SO-30.",
    "Keep the deadline changed. I authorize 800 USD for SO-30.",
    "Keep the new supplier. I authorize 800 USD for SO-30.",
    "Keep SO-31 unapproved. I authorize 800 USD for SO-30.",
    "SO-31 already has its own approved budget. I authorize 800 USD for SO-30; keep its cap at a new 900 USD.",
])
def test_preservation_language_cannot_hide_a_second_change_or_wrong_authority(text):
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "budget_authorization_proposal"))
    assert interpret(text)["kind"] != "budget_authorization_proposal"


def test_source_reference_is_still_required_for_current_amount_after_repair():
    text = "My answer now is 800 USD."
    history = ["I authorize 800 USD for SO-30."]
    proposal = proposed(text, "budget_authorization_proposal")
    proposal["source_spans"][0] = {"reply_id": "old-0", "start": 0, "end": len(history[0])}
    with pytest.raises(v2.ClarificationError, match="current_amount_span_required"):
        admit(text, proposal, history)
    wrong = copy.deepcopy(proposal)
    wrong["source_spans"][0] = {"reply_id": "future", "start": 0, "end": 3}
    with pytest.raises(v2.ClarificationError, match="invisible_source_span"):
        admit(text, wrong, history)


@pytest.mark.parametrize("kind,prefix", [
    ("budget_authorization_proposal", "I authorize 800 USD for SO-30."),
    ("budget_authorization_proposal", "My answer now is 800 USD."),
    ("cancel", "End this recovery case now."),
    ("withdraw_budget", "Withdraw my pending budget candidate."),
])
@pytest.mark.parametrize("revocation", [
    "That was only an example.",
    "I take it back.",
    "Disregard that instruction.",
    "That instruction is not valid.",
])
def test_later_explicit_revocation_applies_to_the_current_statement(kind, prefix, revocation):
    text = prefix + " " + revocation
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, kind))
    assert interpret(text)["kind"] == "ask_followup"


@pytest.mark.parametrize("revocation", [
    "That answer is withdrawn.",
    "Ignore that answer.",
    "That answer is not valid.",
    "Do not use that answer.",
    "This is not my actual answer.",
])
def test_current_answer_cannot_survive_its_own_explicit_denial(revocation):
    text = "My answer now is 800 USD. " + revocation
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, "budget_authorization_proposal"))
    assert interpret(text)["kind"] == "ask_followup"


@pytest.mark.parametrize("text", [
    "My answer now is 800 USD. Keep the other order at its new cap of USD 900.",
    "My answer now is 800 USD. Preserve the newly reduced margin of 10 percent.",
    "My answer now is 800 USD. Keep the changed delivery date.",
    "Withdraw my budget candidate and cancel the case for SO-31.",
    "End this recovery case now and cancel the SO-31 case too.",
])
def test_an_allowed_prefix_cannot_hide_a_later_wrong_scope(text):
    for kind in ("budget_authorization_proposal", "withdraw_budget", "cancel", "pause"):
        with pytest.raises(v2.ClarificationError):
            admit(text, proposed(text, kind))
    assert interpret(text)["kind"] == "ask_followup"


@pytest.mark.parametrize("text,kind", [
    ("My answer now is 800 USD. For example, withdraw the budget candidate.", "budget_authorization_proposal"),
    ("My answer now is 800 USD. Yesterday I withdrew the budget candidate.", "budget_authorization_proposal"),
    ("For example, my answer now is 700 USD; My answer now is 800 USD.", "budget_authorization_proposal"),
    ("Yesterday I wrote, cancel this recovery case now. My answer now is 800 USD.", "budget_authorization_proposal"),
    ("End this recovery case now. Yesterday I withdrew the budget candidate.", "cancel"),
    ("End this recovery case now. For example, authorize 800 USD for SO-30.", "cancel"),
    ("Yesterday I wrote, cancel this recovery case. Withdraw my pending budget candidate.", "withdraw_budget"),
    ("Withdraw my pending budget candidate. For example, cancel this recovery case.", "withdraw_budget"),
])
def test_independent_history_or_example_does_not_revoke_a_real_current_decision(text, kind):
    assert admit(text, proposed(text, kind))["kind"] == kind
    actual = interpret(text)
    assert actual["kind"] == kind
    if kind == "budget_authorization_proposal":
        assert actual["amount"] == "800.00"


@pytest.mark.parametrize("scope", ["For example", "Yesterday I wrote"])
@pytest.mark.parametrize("separator", [", ", " and "])
@pytest.mark.parametrize("command,kind", [
    ("end this recovery case now", "cancel"),
    ("withdraw my pending budget candidate now", "withdraw_budget"),
])
def test_inner_current_control_does_not_escape_its_reported_scope(scope, separator, command, kind):
    text = scope + separator + command + "."
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, kind))
    assert interpret(text)["kind"] == "ask_followup"


@pytest.mark.parametrize("text", [
    "My answer now is 800 USD. Leave SO-31 unchanged; set the delivery date to Friday.",
    "My answer now is 800 USD. Keep the supplier unchanged and replace it.",
    "My answer now is 800 USD. Do not cancel the case while changing the supplier.",
    "Pending final approval, my answer now is 800 USD.",
    "My answer now is 800 USD, but I am not sure.",
    "Maybe, my answer now is 800 USD.",
    "Maybe, end this recovery case now.",
    "Perhaps, withdraw my pending budget candidate.",
])
def test_current_decision_with_explicit_uncertainty_or_unresolved_other_field_is_not_admitted(text):
    for kind in ("budget_authorization_proposal", "withdraw_budget", "cancel", "pause"):
        with pytest.raises(v2.ClarificationError):
            admit(text, proposed(text, kind))
    assert interpret(text)["kind"] == "ask_followup"


@pytest.mark.parametrize("kind,prefix", [
    ("budget_authorization_proposal", "My answer now is 800 USD."),
    ("cancel", "End this recovery case now."),
    ("withdraw_budget", "Withdraw my pending budget candidate."),
])
def test_explicit_change_of_mind_does_not_leave_the_first_action_authorized(kind, prefix):
    text = prefix + " No, I changed my mind."
    with pytest.raises(v2.ClarificationError):
        admit(text, proposed(text, kind))
    assert interpret(text)["kind"] == "ask_followup"

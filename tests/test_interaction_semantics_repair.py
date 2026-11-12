"""Literal repaired-path regressions; old evidence and its scores stay intact."""
import pytest

from forgeflow.investigation import clarification_v2 as v2
from forgeflow.recovery.storage import digest


def ticket(order="30"):
    return {"order_id": order, "order_name": f"SO-{order}",
            "kind": "set_missing_order_budget", "status": "open"}


def check(text, kind, *, amount=None, order="30", history=()):
    visible = [{"reply_id": f"earlier-{index}", "text": previous, "sha256": digest(previous)}
               for index, previous in enumerate(history)]
    visible.append({"reply_id": "current", "text": text, "sha256": digest(text)})
    proposal = {"schema_version": 2, "kind": kind,
                "source_spans": [{"reply_id": "current", "start": 0, "end": len(text)}]}
    if kind == "budget_authorization_proposal":
        proposal.update(order_id=order, currency="USD", amount=amount)
    args = dict(visible_replies=visible, current_reply_id="current")
    accepted = v2.validate_proposal(ticket(order), text, proposal, "USD", **args)
    interpreted = v2.interpret_reply(ticket(order), text, "USD", **args)
    assert interpreted["kind"] == accepted["kind"] == kind
    if amount is not None:
        assert interpreted["amount"] == accepted["amount"] == amount


@pytest.mark.parametrize("text,kind,amount,order,history", [
    ("My answer now is 1680 USD.", "budget_authorization_proposal", "1680.00", "427",
     ["I authorize 1560 USD for SO-427.", "I withdraw that amount; the case remains open."]),
    ("My new answer for SO-1 is 1870 USD.", "budget_authorization_proposal", "1870.00", "1",
     ["My authorized SO-1 budget cap is USD 1960.", "Withdraw my pending budget candidate. Keep the case open."]),
    ("Withdraw my pending budget candidate. Keep the case open.", "withdraw_budget", None, "1", []),
    ("Remove the budget candidate from my previous reply; keep investigating the same case.",
     "withdraw_budget", None, "427", ["I authorize 1770 USD for SO-427."]),
    ("Remove any pending budget candidate. I have no approved amount for SO-1.", "withdraw_budget", None, "1", []),
    ("SO-2 already has its own approved budget. For the open SO-1 ticket I authorize USD 1940; "
     "leave that other order unchanged.", "budget_authorization_proposal", "1940.00", "1", []),
    ("End this recovery case now. Cancel it entirely.", "cancel", None, "1", []),
])
def test_original_failed_expressions_are_explicit_regressions(text, kind, amount, order, history):
    check(text, kind, amount=amount, order=order, history=history)


@pytest.mark.parametrize("text", [
    "My current answer for SO-30 is 800 USD.",
    "SO-31 already has its own approved budget. SO-32 already has its own approved budget. "
    "My answer now is 800 USD; keep SO-31 and SO-32 budgets unchanged.",
    "My answer now is 800 USD. Do not withdraw my pending budget candidate.",
    "My answer now is 800 USD. For example, that answer is withdrawn.",
    "My answer now is 800 USD. Yesterday I changed my mind.",
])
def test_distinct_background_does_not_create_or_revoke_current_amount(text):
    check(text, "budget_authorization_proposal", amount="800.00")


@pytest.mark.parametrize("text", [
    "SO-31 already has its own approved budget. My answer now is 800 USD; leave SO-32 unchanged.",
    "SO-31 already has its own approved budget. SO-32 already has its own approved budget. "
    "My answer now is 800 USD; leave that other order unchanged.",
    "SO-31 already has its own approved budget. My answer now is 800 USD; do not keep SO-31 unchanged.",
    "SO-31 already has its own approved budget. My answer now is 800 USD. Yesterday I wrote: leave SO-31 unchanged.",
    "SO-31 already has its own approved budget. My answer now is 800 USD. For example, keep SO-31 unchanged.",
    "SO-31 already has its own approved budget. My answer now is 800 USD; if confirmed, keep SO-31 unchanged.",
    "SO-31 already has its own approved budget of 900 USD. My answer now is 800 USD; leave SO-31 unchanged.",
    "My answer now is 800 USD; keep SO-31 at USD 900 unchanged.",
    "My answer now is 800 USD; keep SO-30 at its existing cap of 900 USD.",
    "My answer now is 800 USD; keep SO-30 budget unchanged.",
    "My answer now is 800 USD; keep the current budget unchanged.",
    "My answer now is 800 USD; keep all orders unchanged.",
    "My answer now is 800 USD; keep the supplier unchanged and set it to Acme.",
    "My answer now is 800 USD; I approve the new supplier.",
    "My answer now is 800 USD. Set the supplier to Acme while keeping the deadline unchanged.",
    "My answer now is 800 USD. Assign the supplier to Acme while keeping the deadline unchanged.",
    "My answer now is 800 USD. Keep the supplier assigned to Acme while retaining the existing budget.",
    "My answer now is 800 USD; 900 USD.",
    "My answer now is 800 USD; an unexplained value of 900.",
    "End this recovery case now. Keep the case open.",
    "Keep the case open. End this recovery case now.",
    "End this recovery case now. Keep investigating the same case.",
    "End this recovery case now. Keep the deadline unchanged. Cancel it entirely.",
    "Withdraw the budget candidate. Keep the case open. Cancel it entirely.",
    "End this recovery case now. The budget candidate is unconfirmed. Cancel it entirely.",
])
def test_additional_scope_or_unresolved_reference_requires_clarification(text):
    visible = [{"reply_id": "current", "text": text, "sha256": digest(text)}]
    args = dict(visible_replies=visible, current_reply_id="current")
    assert v2.interpret_reply(ticket(), text, "USD", **args)["kind"] == "ask_followup"
    for kind in ("budget_authorization_proposal", "withdraw_budget", "pause", "cancel"):
        proposal = {"schema_version": 2, "kind": kind,
                    "source_spans": [{"reply_id": "current", "start": 0, "end": len(text)}]}
        if kind == "budget_authorization_proposal":
            proposal.update(order_id="30", amount="800.00", currency="USD")
        with pytest.raises(v2.ClarificationError):
            v2.validate_proposal(ticket(), text, proposal, "USD", **args)


def test_example_retraction_cannot_revoke_a_distinct_current_case_control():
    check("End this recovery case now. For example, that was only an example.", "cancel")

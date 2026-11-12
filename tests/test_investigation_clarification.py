"""Handwritten reply expectations; no production parser constructs the oracle."""

import copy

import pytest

from forgeflow.investigation.clarification import (
    ClarificationError,
    interpret_reply,
    validate_proposal,
)

TICKET = {"kind": "set_missing_order_budget", "status": "open", "order_id": "30", "order_name": "SO-30"}


def budget(amount="800.00", **fields):
    return {"kind": "budget_authorization_proposal", "order_id": "30", "amount": amount,
            "currency": "USD", **fields}


@pytest.mark.parametrize("text,amount", [
    ("SO-30 最多批准 800，其他条件照旧。", "800.00"),
    ("Budget for SO-30 is 800 USD.", "800.00"),
    ("I authorize USD800 for sales order ID 30.", "800.00"),
    ("800USD is the cap for SO-30 (id=30).", "800.00"),
    ("销售订单编号30的预算是800美元。", "800.00"),
    ("800 USD", "800.00"),
    ("USD800", "800.00"),
    ("800USD", "800.00"),
    ("改成 900。", "900.00"),
    ("Make that 900 USD.", "900.00"),
    ("Change to 900 USD.", "900.00"),
    ("Not 800; make it 900.", "900.00"),
    ("Change 800 to 900.", "900.00"),
    ("900 instead of 800.", "900.00"),
    ("不是800而是900。", "900.00"),
    ("Supplier quote 600, but the budget is 800.", "800.00"),
    ("供应商报价600，预算800。", "800.00"),
    ("The quote is 600 and the budget is 800.", "800.00"),
    ("SO-30 budget is 1,200.50 USD.", "1200.50"),
    ("SO-30 budget cap 30 USD", "30.00"),
    ("Up to 800 USD.", "800.00"),
    ("No more than 800 USD.", "800.00"),
    ("Don't pause. Budget is 800.", "800.00"),
])
def test_clear_reply_forms_exact_unconfirmed_single_field_proposal(text, amount):
    before = copy.deepcopy(TICKET)
    result = interpret_reply(TICKET, text, "USD")
    assert result == budget(amount)
    assert validate_proposal(TICKET, text, result, "USD") == result
    assert TICKET == before


@pytest.mark.parametrize("text,kind", [
    ("供应商报价是800。", "price_fact"),
    ("The unit price is 800 USD.", "price_fact"),
    ("Revenue is 800.", "price_fact"),
    ("两张订单加起来最多800。", "ambiguous"),
    ("SO-30 and SO-31 have a combined total of 800.", "ambiguous"),
    ("Both orders have a budget of 800.", "ambiguous"),
    ("Budget 800 or 900.", "ambiguous"),
    ("Maybe 800 USD.", "ambiguous"),
    ("Is 800 enough?", "ambiguous"),
    ("Budget is -800.", "ambiguous"),
    ("Budget is 0.", "ambiguous"),
    ("Budget is 1.234.", "ambiguous"),
    ("Use the previous amount.", "ambiguous"),
    ("I received 800 units.", "ambiguous"),
    ("Do not change 800 to 900.", "ambiguous"),
    ("Cancel this case. Budget 800.", "ambiguous"),
    ("SO-31 budget is 800.", "out_of_scope"),
    ("SO-30 (id=31) budget is 800.", "out_of_scope"),
    ("预算800，同时把毛利降到10%。", "out_of_scope"),
    ("Budget 800; margin 10%.", "out_of_scope"),
    ("Budget 800 if you remove SO-31.", "out_of_scope"),
    ("Budget 800. Execute the plan.", "out_of_scope"),
    ("Budget 800 and change supplier.", "out_of_scope"),
    ("Budget is 800 EUR.", "ambiguous"),
    ("Budget is 800 XYZ.", "ambiguous"),
    ("先暂停。", "pause"),
    ("I will decide later.", "pause"),
    ("Cancel this case.", "cancel"),
    ("取消本次任务", "cancel"),
])
def test_non_authorization_replies_do_not_silently_fill_the_budget(text, kind):
    result = interpret_reply(TICKET, text, "USD", history=[{"text": "Earlier budget 800"}])
    assert result["kind"] == kind
    assert set(result) == {"kind", "question"}
    assert validate_proposal(TICKET, text, result, "USD") == result


@pytest.mark.parametrize("text,proposal", [
    ("Budget 800", budget(order_id="31")),
    ("Budget 800", budget(currency="EUR")),
    ("Quote 800", budget()),
    ("Quote 800; budget 900", budget()),
    ("Not 800; make it 900", budget()),
    ("SO-31 budget 800", budget()),
    ("SO-30 (ID:31) budget 800", budget()),
    ("Two orders combined budget 800", budget()),
    ("预算800，毛利10%", budget()),
    ("Budget 800 if the margin changes", budget()),
    ("Maybe budget 800", budget()),
    ("Use the previous amount", budget()),
    ("Pause, budget 800", budget()),
    ("Budget 800 EUR", budget()),
    ("Budget 800 XYZ", budget()),
    ("I received 800 units", budget()),
    ("Do not change 800 to 900", budget("900")),
    ("Cancel this case. Budget 800", budget()),
    ("Budget -800", budget()),
    ("Budget 800", budget("1,23")),
    ("Budget 800", budget(True)),
    ("Budget 800", budget(margin=0.1)),
    ("Budget 800", {"kind": "cancel"}),
    ("Budget 800", {"kind": "pause"}),
    ("Quote 800", {"kind": "price_fact", "amount": "800"}),
])
def test_shared_model_admission_rejects_obvious_wrong_grants(text, proposal):
    with pytest.raises(ClarificationError):
        validate_proposal(TICKET, text, proposal, "USD")


def test_common_gate_does_not_use_rule_classification_as_a_model_oracle():
    text = "The quotation is 800."
    assert interpret_reply(TICKET, text, "USD")["kind"] == "price_fact"
    different_safe_path = {"kind": "ambiguous", "question": "Do you authorize a budget for SO-30?"}
    assert validate_proposal(TICKET, text, different_safe_path, "USD") == different_safe_path


def test_history_never_contributes_the_authorization_amount():
    result = interpret_reply(TICKET, "Yes, use that.", "USD", history=[{"text": "Budget 800"}])
    assert result["kind"] == "ambiguous"
    with pytest.raises(ClarificationError, match="budget_decision_unproven"):
        validate_proposal(TICKET, "Yes, use that.", budget(), "USD")


def test_same_numeric_order_id_cannot_be_used_as_a_budget():
    with pytest.raises(ClarificationError, match="budget_decision_unproven"):
        validate_proposal(TICKET, "sales order ID 30", budget("30"), "USD")


@pytest.mark.parametrize("currency,text", [("EUR", "预算800欧元"), ("CNY", "预算800元"), ("JPY", "预算800日元")])
def test_fixed_case_currency_is_preserved_without_conversion(currency, text):
    result = interpret_reply(TICKET, text, currency)
    assert result == budget(currency=currency)


def test_closed_ticket_cannot_create_a_new_proposal():
    with pytest.raises(ClarificationError, match="invalid_clarification_ticket"):
        interpret_reply({**TICKET, "status": "confirmed"}, "Change to 900", "USD")

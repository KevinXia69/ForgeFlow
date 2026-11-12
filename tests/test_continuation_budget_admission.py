"""Independent live-failure regressions; literal expected outcomes, no model calls."""
import copy

import pytest
from test_investigation_continuation import make_system, waiting

from forgeflow.investigation.cases import CaseError
from forgeflow.investigation.clarification import (
    ClarificationError,
    interpret_reply,
    validate_proposal,
)
from forgeflow.recovery.storage import read_json

TICKET = {'kind': 'set_missing_order_budget', 'status': 'open',
          'order_id': '30', 'order_name': 'SO-30'}


def proposal(amount='3872.00'):
    return {'kind': 'budget_authorization_proposal', 'order_id': '30',
            'amount': amount, 'currency': 'USD'}


@pytest.mark.parametrize('text,amount', [
    ('The supplier quoted 3872.00 USD. I have not authorized a budget for SO-30.', '3872.00'),
    ('The supplier quoted 3872.00 USD. A budget decision is still needed.', '3872.00'),
    ('The supplier estimated 3872 USD. Please ask me for the budget.', '3872.00'),
    ('The supplier priced this at 3872 USD. No budget has been approved.', '3872.00'),
    ('I received 3872 units. I authorize a budget, but have not stated the cap.', '3872.00'),
    ('3872 USD. I have not authorized any budget.', '3872.00'),
    ('供应商报价3872美元。我尚未授权SO-30的预算。', '3872.00'),
    ('预计成本3872美元，预算尚未批准。', '3872.00'),
    ('I authorize 3872 USD. I retract that authorization.', '3872.00'),
    ('Budget 3872 USD. Withdraw that budget.', '3872.00'),
    ('Budget 3872 USD. That authorization is revoked.', '3872.00'),
    ('Budget 3872 USD. I no longer authorize that amount.', '3872.00'),
    ('预算3872美元。该授权已撤回。', '3872.00'),
    ('预算3872美元。预算不予批准。', '3872.00'),
    ('Budget 3872 USD. That is not my budget.', '3872.00'),
    ('I have not authorized a budget. The cap is 3872 USD.', '3872.00'),
    ('预算3872美元。撤回该授权。', '3872.00'),
    ('SO-30预算3872美元。我没有批准这个预算。', '3872.00'),
    ('For both target orders together, the maximum budget is 2600 USD.', '2600.00'),
    ('For all affected sales orders, budget 2600 USD.', '2600.00'),
    ('两张目标订单合计预算2600美元。', '2600.00'),
    ('全部受影响订单的预算合计2600美元。', '2600.00'),
    ('The supplier quoted a budget of 3872 USD.', '3872.00'),
    ('The quote is 3872 and I authorize the same amount as budget.', '3872.00'),
    ('The budget is 3872 or 4000 USD.', '3872.00'),
    ('The budget for SO-30 is 3872 USD and 4000 USD.', '3872.00'),
    ('I authorize 3872 USD and 4000 USD.', '3872.00'),
    ('I authorize 3872 USD and 4000 USD for SO-30.', '3872.00'),
    ('I authorize 3872 USD and 4000 USD on this order.', '3872.00'),
    ('I authorize 3872 USD and 4000 USD for this order.', '3872.00'),
    ('I authorize 3872 USD. 4000 USD for SO-30.', '3872.00'),
    ('4000 USD on this order. I authorize 3872 USD.', '3872.00'),
    ('Budget 3872 USD. I revoked that authorization.', '3872.00'),
    ('Budget 3872 USD. I have not allocated a budget.', '3872.00'),
    ('Budget approval of 3872 USD for SO-30 was rejected.', '3872.00'),
    ('预算3872美元，该授权已作废。', '3872.00'),
])
def test_unproven_or_withdrawn_amount_never_becomes_a_confirmable_proposal(text, amount):
    with pytest.raises(ClarificationError):
        validate_proposal(TICKET, text, proposal(amount), 'USD')
    assert interpret_reply(TICKET, text, 'USD')['kind'] != 'budget_authorization_proposal'


@pytest.mark.parametrize('text,amount', [
    ('The supplier quoted 3872 USD. I authorize a budget cap of 5100 USD for SO-30.', '5100.00'),
    ('The estimate is 3872 USD and the approved budget for SO-30 is 5100 USD.', '5100.00'),
    ('The supplier priced it at 3872, but the budget cap is 5100.', '5100.00'),
    ('SO-30的报价为3872美元，预算授权为5100美元。', '5100.00'),
    ('3872 USD', '3872.00'),
    ('3872', '3872.00'),
    ('Budget cap for SO-30 is 30 USD.', '30.00'),
    ('5100 USD is the budget cap for SO-30.', '5100.00'),
    ('The quotation is 3872. Change the budget to 5100 USD.', '5100.00'),
    ('Not 3872; make it 5100.', '5100.00'),
    ('Change 3872 to 5100.', '5100.00'),
    ('不是3872而是5100。', '5100.00'),
    ('That earlier total was not an allocation. For SO-30 alone, I authorize a budget cap of 5100 USD.', '5100.00'),
    ('No more than 5100 USD.', '5100.00'),
    ('The supplier quote was not approved. I authorize a budget of 5100 USD.', '5100.00'),
    ('The quotation is 3872, not my authorization. I authorize a budget of 5100 USD.', '5100.00'),
])
def test_local_explicit_budget_or_correction_remains_accepted(text, amount):
    expected = proposal(amount)
    assert validate_proposal(TICKET, text, expected, 'USD') == expected
    assert interpret_reply(TICKET, text, 'USD') == expected


@pytest.mark.parametrize('text', [
    'The supplier quoted 3872 USD. I have not authorized a budget for SO-30.',
    'For both target orders together, the maximum budget is 3872 USD.',
    'Budget 3872 USD. I retract that authorization.',
])
def test_real_case_rejects_unsafe_proposal_without_policy_child_or_confirmation(tmp_path, text):
    system = make_system(tmp_path)
    view = waiting(system)
    cases = system['cases']
    reply = cases.submit_reply(view['case_id'], 'owner', operation_id='raw-answer',
                               expected_revision=view['revision'], text=text)
    directory = cases._directory(view['case_id'])
    before = read_json(directory / 'case.json')
    business_before = copy.deepcopy(system['source'].records)
    before_files = {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
    with pytest.raises(CaseError):
        cases.propose(view['case_id'], 'owner', reply_id=reply['reply_id'], proposal=proposal())
    after = read_json(directory / 'case.json')
    assert after == before
    assert after['confirmation'] is None and after['continuation'] is None
    assert len(after['episodes']) == 1
    assert {str(p.relative_to(directory)): p.read_bytes() for p in directory.rglob('*') if p.is_file()} == before_files
    assert system['policy'].payload()['orders']['30']['budget_cap'] is None
    assert business_before == system['source'].records


@pytest.mark.parametrize('kind', ['price_fact', 'ambiguous', 'out_of_scope', 'pause', 'cancel'])
def test_non_budget_schema_is_still_strict_no_empty_or_null_budget_fields(kind):
    text = 'Cancel this case.' if kind == 'cancel' else 'Pause this case.' if kind == 'pause' else 'The supplier quoted 3872 USD.'
    assert validate_proposal(TICKET, text, {'kind': kind, 'question': ''}, 'USD')['kind'] == kind
    for field in ['order_id', 'amount', 'currency']:
        for value in [None, '', '3872']:
            with pytest.raises(ClarificationError, match='invalid_reply_proposal'):
                validate_proposal(TICKET, text, {'kind': kind, field: value}, 'USD')


def test_real_worker_dispatch_explains_shape_without_reclassifying_factual_amount(tmp_path, monkeypatch):
    from test_investigation_worker import worker

    w = worker(tmp_path)
    text = 'The supplier quoted 3872 USD. I have not authorized a budget for SO-30.'
    calls = []

    def fake_provider_boundary(*, dispatch, **kwargs):
        # Mock only provider execution, retain the real reply dispatch/validator.
        assert kwargs['mode'] == 'reply'
        malformed = dispatch('proposal', {'kind': 'price_fact', 'amount': '3872',
                                          'order_id': '30', 'currency': 'USD'})
        assert malformed['accepted'] is False and malformed['reason'] == 'invalid_reply_proposal'
        assert 'omit order_id, amount and currency' in malformed['instruction']
        assert 'do not change classification' in malformed['instruction']
        forged = dispatch('proposal', proposal())
        assert forged['accepted'] is False
        corrected = dispatch('proposal', {'kind': 'price_fact', 'question': 'What cap do you authorize for SO-30?'})
        assert corrected['accepted'] is True and corrected['authorization_created'] is False
        calls.extend([malformed, forged, corrected])
        return {'seconds': 0, 'runtime_valid': True}, {'tokens': 0}

    monkeypatch.setattr(w, '_model', fake_provider_boundary)
    outcome = w.reply(ticket=TICKET, reply={'text': text, 'reply_id': 'r1', 'sha256': 'a'*64},
                      currency='USD', history=[], strategy='interactive', allowance={}, work_directory=tmp_path)
    assert outcome['proposal']['kind'] == 'price_fact'
    assert len(calls) == 3
    assert read_json(tmp_path / 'proposal.json') == outcome['proposal']


@pytest.mark.parametrize('text', [
    'Authorize budget 800 USD for S00004.',
    'Authorize budget 800 USD for sales order S00004.',
    '为销售订单S00004授权预算800美元。',
    '给S00004的预算是800美元。',
    'For S00004, the cap is 800 USD; S00001 remains unchanged.',
])
def test_actual_odoo_business_name_cannot_redirect_the_budget(text):
    ticket = {**TICKET, 'order_id': '1', 'order_name': 'S00001'}
    draft = {**proposal('800.00'), 'order_id': '1'}
    with pytest.raises(ClarificationError, match='wrong_budget_order'):
        validate_proposal(ticket, text, draft, 'USD')
    assert interpret_reply(ticket, text, 'USD')['kind'] == 'out_of_scope'


@pytest.mark.parametrize('text,amount', [
    ('Authorize budget 800 USD for S00001.', '800.00'),
    ('为销售订单S00001授权预算800美元。', '800.00'),
    ('The SKU S00004 is unchanged. Budget for S00001 is 800 USD.', '800.00'),
    ('PO S00004 is unchanged. Budget for S00001 is 800 USD.', '800.00'),
    ('S00001 budget cap is 1 USD.', '1.00'),
])
def test_actual_odoo_name_is_distinct_from_sku_po_and_same_numeric_cap(text, amount):
    ticket = {**TICKET, 'order_id': '1', 'order_name': 'S00001'}
    expected = {**proposal(amount), 'order_id': '1'}
    assert validate_proposal(ticket, text, expected, 'USD') == expected
    assert interpret_reply(ticket, text, 'USD') == expected


@pytest.mark.parametrize('text,accepted', [
    ('Budget 900 USD for both selected target orders.', False),
    ('Budget 900 USD for all the affected sales orders.', False),
    ('Budget 900 USD for both of the target orders.', False),
    ('Budget 900 USD for all of the selected affected sales orders.', False),
    ('For SO-30 only, I authorize budget 900 USD; other budgets remain unchanged.', True),
])
def test_total_order_modifiers_do_not_disguise_a_combined_allocation(text, accepted):
    expected = proposal('900.00')
    if accepted:
        assert validate_proposal(TICKET, text, expected, 'USD') == expected
        assert interpret_reply(TICKET, text, 'USD') == expected
    else:
        with pytest.raises(ClarificationError, match='combined_budget_ambiguous'):
            validate_proposal(TICKET, text, expected, 'USD')
        assert interpret_reply(TICKET, text, 'USD')['kind'] == 'ambiguous'

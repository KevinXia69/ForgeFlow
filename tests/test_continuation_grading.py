"""Hand-authored grading counterexamples independent of reply implementation."""
import copy

import pytest
from test_continuation_development_dataset import runner

from forgeflow.recovery.storage import atomic_json

TICKET = {'order_id': '27', 'order_name': 'SO-42'}


@pytest.mark.parametrize('question', [
    'What budget cap do you authorize for SO-42 (sales order ID 27)?',
    'Please provide the budget for this order in USD.',
    '请提供 SO-42（编号27）的预算上限。',
    'A quote is not spending authorization. What budget do you authorize for SO-42?',
])
def test_matching_questions_get_predeclared_budget_reply(question):
    assert runner.budget_question_for_ticket(question, TICKET)


@pytest.mark.parametrize('question', [
    'What budget for SO-999?', 'What budget for order 999?',
    'What cap for SO-42 (id=999)?', 'Please give the budget for both orders.',
    'What budget for SO-42? Which supplier cancelled?',
    'What budget cap and what price should be used for SO-42?',
    'Please confirm a budget of 999 USD for SO-42.',
    '请确认 SO-42 的预算999元。', 'Which supplier has the quote?',
])
def test_wrong_objects_proposed_grants_and_unnecessary_fact_questions_do_not_get_helpful_reply(question):
    assert not runner.budget_question_for_ticket(question, TICKET)


@pytest.mark.parametrize('question,expected', [
    ('What budget cap do you authorize for S00001 (sales order ID 1)?', True),
    ('What budget cap do you authorize for S00004 (sales order ID 1)?', False),
    ('Please provide the budget for S00001.', True),
    ('What budget for both target orders?', False),
    ('What cap for all affected sales orders?', False),
    ('Please give a budget for each selected order.', False),
    ('What cap for all the affected sales orders?', False),
    ('What budget for both of the target orders?', False),
])
def test_live_order_names_and_plural_target_scope(question, expected):
    assert runner.budget_question_for_ticket(question, {'order_id': '1', 'order_name': 'S00001'}) is expected


def test_claimed_worker_without_provider_receipt_is_unknown_not_zero(tmp_path):
    atomic_json(tmp_path / 'case.json', {'work': {'episode-1': {'kind': 'parent', 'phase': 'reserved'}}})
    metrics = runner.provider_metrics(tmp_path)
    assert metrics['unknown_workers'] == 1 and metrics['possible_provider_workers'] == 1
    assert metrics['provider_workers'] == 0 and metrics['usage_complete'] is False
    assert metrics['usage_by_field']['input'] is None
    assert metrics['known_prefix_usage']['input'] == 0


def test_complete_prefix_does_not_make_missing_other_worker_usage_complete(tmp_path):
    atomic_json(tmp_path / 'case.json', {'work': {'episode-1': {'kind': 'parent', 'phase': 'complete'},
                                                  'reply-1': {'kind': 'reply', 'phase': 'reserved'}}})
    directory = tmp_path / 'workers/episode-1'
    directory.mkdir(parents=True)
    atomic_json(directory / 'runtime.json', {'dispatch_started': True})
    atomic_json(directory / 'provider-usage.json', {'tokens': 125, 'messages': [
        {'usage': {'input': 100, 'output': 25, 'cacheRead': 0, 'cacheWrite': 0}}]})
    metrics = runner.provider_metrics(tmp_path)
    assert metrics['unknown_workers'] == 1
    assert metrics['usage_by_field']['input'] is None
    assert metrics['known_prefix_usage']['input'] == 100


def test_rule_work_does_not_invent_unknown_model_usage(tmp_path):
    atomic_json(tmp_path / 'case.json', {'work': {'episode-1': {'kind': 'parent', 'phase': 'complete'}}})
    metrics = runner.provider_metrics(tmp_path, uses_model=False)
    assert metrics['provider_workers'] == 0 and metrics['unknown_workers'] == 0
    assert metrics['usage_by_field']['input'] == 0


def test_wrong_order_followup_is_script_unmatched():
    view = {'status': 'awaiting_human', 'ticket': copy.deepcopy(TICKET), 'replies': [{'text': 'some quote'}],
            'draft': {'proposal': {'kind': 'ambiguous', 'question': 'What budget for SO-999?'}}}
    assert runner.script_trigger(view) == 'unmatched_question'

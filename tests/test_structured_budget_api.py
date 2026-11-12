"""Structured HTTP decisions exercise the real case aggregate, without models or ERP."""
import copy
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from investigation_support import EVENT, make_case
from test_investigation_api import INVITE, OTHER, confirmation, login
from test_investigation_continuation import partial

from forgeflow.api.app import create_app
from forgeflow.api.investigation import InvestigationAdapter
from forgeflow.api.settings import create_settings
from forgeflow.contract import contract_from_payload
from forgeflow.investigation.cases import InvestigationCases
from forgeflow.investigation.continuation import ContinuationCoordinator
from forgeflow.investigation.host import ContinuationHost
from forgeflow.investigation.intake import task_template
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.service import InvestigationService
from forgeflow.investigation.sources import FixtureSource
from forgeflow.recovery.storage import read_json


@pytest.fixture
def structured_api(tmp_path, monkeypatch):
    source, original, _ = make_case((30, 31))
    payload = original.payload()
    payload['expires_at_utc'] = '1970-01-01T00:03:20+00:00'
    contract = contract_from_payload(payload)
    policy, clock, calls = partial(contract), [100], []
    environment = {'generation': 'structured-api-generation', 'currency': 'USD',
                   'mode': 'structured_budget_v1'}
    initial = copy.deepcopy(source.records)

    def factory(value):
        return FixtureSource(source.records, value.source_policy() if isinstance(value, PartialPolicy) else value)

    cases = InvestigationCases(tmp_path / 'cases', source_factory=factory,
        generation=lambda: environment['generation'], clock=lambda: clock[0],
        currency_probe=lambda _: {'currency': 'USD', 'company_id': 1, 'provenance': 'fixture',
                                 'record_currencies': {'orders-and-offers': environment['currency']}})
    host = ContinuationHost(cases=cases, product=None, create_inputs=lambda _: {},
        coordinator=ContinuationCoordinator(cases,
            episode_runner=lambda **_: pytest.fail('No model dispatch in HTTP boundary tests'),
            reply_interpreter=lambda **_: pytest.fail('No reply model for structured input')))

    def create(principal, operation_id):
        options = {'decision_input_mode': environment['mode']}
        view = cases.create(principal, operation_id=operation_id, template=task_template(contract),
                            policy=policy, event=EVENT, **options)
        config = cases.episode_config(view['case_id'], principal, view['current_episode'])
        service = InvestigationService(source=factory(policy), contract=policy, store=config['store'],
            bundle=tmp_path / 'unused', connection_factory=lambda: pytest.fail('No ERP'), event=EVENT)
        service.dispatch(config['run_id'], 'finish', {'status': 'needs_clarification',
            'reason': 'Missing budget', 'question': 'What budget do you authorize for SO-30?'})
        return cases.record_terminal(view['case_id'], principal, config['run_id'])

    def no_language(*args, **kwargs):
        pytest.fail('Structured input must not invoke a language interpreter')

    monkeypatch.setattr('forgeflow.investigation.clarification.interpret_reply', no_language)
    monkeypatch.setattr('forgeflow.investigation.clarification_v2.interpret_reply', no_language)
    adapter = InvestigationAdapter(cases=cases, create_case=create,
        schedule=lambda *args: calls.append(args), background=host.background,
        request_closure=host.request_closure, expire=host.expire, reconcile_closure=host.reconcile_closure)
    settings = create_settings({'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
        'FORGEFLOW_SESSION_SECRET': 'structured-session-secret-0123456789',
        'FORGEFLOW_INVITE_CODES': INVITE, 'FORGEFLOW_SECURE_COOKIE': 'true'})
    settings = replace(settings, invite_codes=(INVITE, OTHER))
    result = {'settings': settings, 'adapter': adapter, 'cases': cases, 'host': host,
              'clock': clock, 'environment': environment, 'calls': calls, 'source': source}
    yield result
    assert source.records == initial


def new_case(client):
    response = client.post('/api/investigation/cases', json={'operation_id': 'create'})
    assert response.status_code == 201, response.text
    return response.json()


def budget_body(view, amount='800.00', operation='input'):
    return {'operation_id': operation, 'expected_revision': view['revision'],
            'ticket_id': view['ticket']['ticket_id'], 'amount': amount}


def control_body(view, action, operation=None):
    return {'operation_id': operation or action, 'expected_revision': view['revision'], 'action': action}


def client_for(system, **changes):
    settings = replace(system['settings'], **changes) if changes else system['settings']
    return TestClient(create_app(settings, investigation_adapter=system['adapter']), base_url='https://testserver')


def test_structured_candidate_is_synchronous_and_confirmation_alone_enqueues_child(structured_api):
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        legacy = client.post(path + '/reply', json={'operation_id': 'wrong-entry',
            'expected_revision': view['revision'], 'text': 'Budget 800 USD'})
        assert legacy.status_code == 409 and legacy.json()['detail'] == 'structured_budget_required'
        assert structured_api['calls'] == []
        body = budget_body(view, '800.0')
        response = client.post(path + '/budget', json=body)
        assert response.status_code == 200, response.text
        candidate = response.json()
        assert candidate['draft']['source'] == 'structured_budget'
        assert candidate['draft']['proposal']['amount'] == '800.00'
        assert candidate['budget_inputs'][-1]['raw_input'] == {'ticket_id': body['ticket_id'], 'amount': '800.0'}
        saved = read_json(structured_api['cases']._directory(view['case_id']) / 'case.json')
        assert candidate['budget_inputs'][-1]['actor'] == saved['owner']
        assert candidate['budget_inputs'][-1]['source'] == 'authenticated_user'
        assert candidate['confirmation'] is None and candidate['replies'] == []
        assert len(candidate['episodes']) == 1 and structured_api['calls'] == []
        assert client.post(path + '/budget', json=body).json() == candidate
        assert client.post(path + '/budget', json={**body, 'amount': '900'}).status_code == 409
        confirmation_body = confirmation(candidate)
        confirmed = client.post(path + '/confirm', json=confirmation_body)
        assert confirmed.status_code == 200, confirmed.text
        assert len(confirmed.json()['episodes']) == 2
        assert confirmed.json()['confirmation']['after'] == '800.00'
        assert client.post(path + '/confirm', json=confirmation_body).json() == confirmed.json()
        assert [call[0] for call in structured_api['calls']] == ['child', 'child']
        assert {call[3] for call in structured_api['calls']} == {'confirm-1'}


def test_correction_pause_resume_and_withdraw_invalidate_old_confirmations(structured_api):
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        first = client.post(path + '/budget', json=budget_body(view)).json()
        old_confirm = confirmation(first)
        revised = client.post(path + '/budget', json=budget_body(first, '900', 'correction')).json()
        assert revised['draft']['draft_sha256'] != first['draft']['draft_sha256']
        assert client.post(path + '/confirm', json=old_confirm).status_code == 409
        pause = control_body(revised, 'pause')
        paused = client.post(path + '/budget-control', json=pause)
        assert paused.status_code == 200, paused.text
        paused = paused.json()
        assert paused['decision_paused'] is True and paused['draft'] is None
        assert client.post(path + '/budget-control', json=pause).json() == paused
        assert client.post(path + '/budget', json=budget_body(paused, '1000', 'while-paused')).status_code == 409
        assert client.post(path + '/confirm', json={**confirmation(revised), 'expected_revision': paused['revision']}).status_code == 409
        resumed = client.post(path + '/budget-control', json=control_body(paused, 'resume')).json()
        assert resumed['decision_paused'] is False and resumed['draft'] is None
        candidate = client.post(path + '/budget', json=budget_body(resumed, '950.25', 'resumed-input')).json()
        withdrawn = client.post(path + '/budget-control', json=control_body(candidate, 'withdraw')).json()
        assert withdrawn['draft'] is None and withdrawn['status'] == 'awaiting_human'
        assert withdrawn['confirmation'] is None and withdrawn['closure_intent'] is None
        assert len(withdrawn['episodes']) == 1 and structured_api['calls'] == []


@pytest.mark.parametrize('amount', [800, 800.25, True, None, '', '0', '-1', 'NaN', 'Infinity', '8e2',
                                   '1,000', '1.001', '1000000000000.01'])
def test_invalid_or_nonstring_amount_never_changes_case(structured_api, amount):
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        response = client.post(path + '/budget', json=budget_body(view, amount))
        assert response.status_code in (400, 422), response.text
        assert client.get(path).json() == view
        assert structured_api['calls'] == []


@pytest.mark.parametrize('extra', ['owner', 'actor', 'generation', 'currency', 'order_id', 'policy',
                                   'contract', 'execute', 'decision_input_mode', 'decision_contract_version'])
def test_browser_cannot_supply_authority_or_scope(structured_api, extra):
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        for endpoint, body in [('/budget', budget_body(view)),
                               ('/budget-control', control_body(view, 'pause'))]:
            response = client.post(path + endpoint, json={**body, extra: 'forged'})
            assert response.status_code == 422
        assert client.get(path).json() == view and structured_api['calls'] == []


def test_authenticated_owner_origin_revision_ticket_and_read_only_are_required(structured_api):
    with client_for(structured_api) as first, client_for(structured_api) as other:
        login(first)
        view = new_case(first)
        path = f"/api/investigation/cases/{view['case_id']}"
        for suffix, body in [('/budget', budget_body(view)), ('/budget-control', control_body(view, 'pause'))]:
            assert other.post(path + suffix, json=body).status_code == 401
        login(other, OTHER)
        for suffix, body in [('/budget', budget_body(view)), ('/budget-control', control_body(view, 'pause'))]:
            assert other.post(path + suffix, json=body).status_code == 404
            assert first.post(path + suffix, json=body, headers={'Origin': 'https://other.example'}).status_code == 403
            assert first.post(path + suffix, json={**body, 'expected_revision': 999}).status_code == 409
        assert first.post(path + '/budget', json={**budget_body(view), 'ticket_id': 'wrong-ticket'}).status_code == 409
        assert first.post(path + '/budget-control', json=control_body(view, 'cancel')).status_code == 422
        assert first.get(path).json() == view
    with client_for(structured_api, deployment='review') as review:
        login(review)
        assert review.post(path + '/budget', json=budget_body(view)).status_code == 403
        assert review.post(path + '/budget-control', json=control_body(view, 'pause')).status_code == 403
    assert structured_api['calls'] == []


def test_background_block_and_expiry_leave_closure_available(structured_api):
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        original = structured_api['adapter'].background
        structured_api['adapter'].background = lambda *_: {
            'status': 'reconciliation_required', 'requires_reconciliation': True, 'jobs': []}
        for suffix, body in [('/budget', budget_body(view)), ('/budget-control', control_body(view, 'pause'))]:
            response = client.post(path + suffix, json=body)
            assert response.status_code == 409 and response.json()['detail'] == 'investigation_reconciliation_required'
        structured_api['adapter'].background = original
        structured_api['clock'][0] = 201
        assert client.post(path + '/budget', json=budget_body(view)).status_code == 409
        assert client.post(path + '/budget-control', json=control_body(view, 'pause')).status_code == 409
        closed = client.post(path + '/close-expired', json={'operation_id': 'expiry'})
        assert closed.status_code == 200, closed.text
        assert closed.json()['environment_released'] and closed.json()['confirmation'] is None
        assert structured_api['calls'] == []


def test_legacy_reply_mode_remains_distinct_from_structured_operations(structured_api):
    structured_api['environment']['mode'] = 'legacy_reply'
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        assert view['decision_input_mode'] == 'legacy_reply'
        assert client.post(path + '/budget', json=budget_body(view)).status_code == 409
        assert client.post(path + '/budget-control', json=control_body(view, 'pause')).status_code == 409
        response = client.post(path + '/reply', json={'operation_id': 'legacy-reply',
            'expected_revision': view['revision'], 'text': 'Budget 800 USD'})
        assert response.status_code == 200, response.text
        assert response.json()['replies'][-1]['text'] == 'Budget 800 USD'
        assert [call[0] for call in structured_api['calls']] == ['reply']


def test_currency_generation_and_old_confirmation_binding_rechecked(structured_api):
    with client_for(structured_api) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        candidate = client.post(path + '/budget', json=budget_body(view)).json()
        structured_api['environment']['currency'] = 'EUR'
        assert client.post(path + '/confirm', json=confirmation(candidate)).status_code == 409
        structured_api['environment']['currency'] = 'USD'
        structured_api['environment']['generation'] = 'another-generation'
        assert client.post(path + '/confirm', json=confirmation(candidate)).status_code == 409
        assert client.post(path + '/budget-control', json=control_body(candidate, 'withdraw')).status_code == 409
        assert client.get(path).json()['confirmation'] is None
        assert read_json(structured_api['cases'].root / 'environment.json')['active_case'] == view['case_id']
        assert structured_api['calls'] == []


def test_hosted_budget_and_control_routes_share_existing_mutation_rate_limit(structured_api):
    structured_api['environment']['generation'] = 'a' * 32
    with client_for(structured_api, deployment='hosted', environment_generation='a' * 32,
                    trusted_hosts=('testserver',)) as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        body = budget_body(view)
        candidate = client.post(path + '/budget', json=body)
        assert candidate.status_code == 200, candidate.text
        for _ in range(18):
            assert client.post(path + '/budget', json=body).status_code == 200
        limited = client.post(path + '/budget-control', json=control_body(candidate.json(), 'pause'))
        assert limited.status_code == 429 and limited.json()['detail'] == 'rate_limit_exceeded'
        assert int(limited.headers['Retry-After']) > 0
        assert client.get(path).json()['decision_paused'] is False
        assert structured_api['calls'] == []

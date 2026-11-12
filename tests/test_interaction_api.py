"""Real closure state through authenticated HTTP; no model or ERP calls."""
from dataclasses import replace

import pytest
from fastapi.testclient import TestClient
from investigation_support import EVENT, make_case
from test_investigation_api import INVITE, OTHER, login
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
def closure_api(tmp_path):
    source, original, _ = make_case((30, 31))
    payload = original.payload()
    payload['expires_at_utc'] = '1970-01-01T00:03:20+00:00'
    contract = contract_from_payload(payload)
    policy, clock, calls = partial(contract), [100], []

    def factory(value):
        return FixtureSource(source.records, value.source_policy() if isinstance(value, PartialPolicy) else value)

    cases = InvestigationCases(tmp_path / 'cases', source_factory=factory,
        generation=lambda: 'api-closure-generation', clock=lambda: clock[0],
        currency_probe=lambda _: {'currency': 'USD', 'company_id': 1, 'provenance': 'fixture',
                                 'record_currencies': {'orders-and-offers': 'USD'}})
    host = ContinuationHost(cases=cases, product=None, create_inputs=lambda _: {},
        coordinator=ContinuationCoordinator(cases, episode_runner=lambda **_: pytest.fail('No worker dispatch'),
                                            reply_interpreter=None))

    def create(principal, operation_id):
        view = cases.create(principal, operation_id=operation_id, template=task_template(contract),
                            policy=policy, event=EVENT)
        config = cases.episode_config(view['case_id'], principal, view['current_episode'])
        service = InvestigationService(source=factory(policy), contract=policy, store=config['store'],
            bundle=tmp_path / 'unused', connection_factory=lambda: pytest.fail('No ERP'), event=EVENT)
        service.dispatch(config['run_id'], 'finish', {'status': 'needs_clarification',
            'reason': 'Missing budget', 'question': 'What budget do you authorize for SO-30?'})
        return cases.record_terminal(view['case_id'], principal, config['run_id'])

    adapter = InvestigationAdapter(cases=cases, create_case=create,
        schedule=lambda *args: calls.append(args), background=host.background,
        request_closure=host.request_closure, expire=host.expire, reconcile_closure=host.reconcile_closure)
    settings = create_settings({'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
        'FORGEFLOW_SESSION_SECRET': 'interaction-session-secret-0123456789', 'FORGEFLOW_INVITE_CODES': INVITE})
    settings = replace(settings, invite_codes=(INVITE, OTHER))
    return settings, adapter, host, clock, calls


def new_case(client, operation='create'):
    response = client.post('/api/investigation/cases', json={'operation_id': operation})
    assert response.status_code == 201, response.text
    return response.json()


def test_expiry_get_is_read_only_but_explicit_close_releases_original_case(closure_api):
    settings, adapter, _, clock, calls = closure_api
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url='https://testserver') as client:
        login(client)
        view = new_case(client)
        path = f"/api/investigation/cases/{view['case_id']}"
        state = adapter.cases._directory(view['case_id']) / 'case.json'
        before = state.read_bytes()
        clock[0] = 201
        assert client.get(path).json()['policy_expired'] is True
        assert state.read_bytes() == before
        reply = client.post(path + '/reply', json={'operation_id': 'reply',
            'expected_revision': view['revision'], 'text': '800 USD'})
        assert reply.status_code == 409 and reply.json()['detail'] == 'policy_expired'
        closed = client.post(path + '/close-expired', json={'operation_id': 'expiry'})
        assert closed.status_code == 200, closed.text
        result = closed.json()
        assert result['status'] == 'closed'
        assert result['closure_intent']['reason'] == 'policy_expired'
        assert result['environment_released'] and result['confirmation'] is None
        assert read_json(adapter.cases.root / 'environment.json')['active_case'] is None
        assert client.post(path + '/close-expired', json={'operation_id': 'expiry'}).json() == result
        assert calls == []


def test_closure_can_be_requested_while_unknown_work_keeps_environment_occupied(closure_api):
    settings, adapter, _, _, calls = closure_api
    with TestClient(create_app(settings, investigation_adapter=adapter), base_url='https://testserver') as client:
        login(client)
        view = new_case(client)
        raw = read_json(adapter.cases._directory(view['case_id']) / 'case.json')
        owner = raw['owner']
        adapter.cases.reserve_work(view['case_id'], owner, operation_id='original-work', kind='reply',
            allowance={'tokens': 100, 'seconds': 100, 'queries': 1, 'validations': 1, 'prepares': 1})
        # Unknown provider outcome is real persisted work, not a mock HTTP label.
        adapter.cases.finish_work(view['case_id'], owner, operation_id='original-work',
                                 usage={'tokens': None, 'seconds': None, 'queries': None,
                                        'validations': None, 'prepares': None})
        view = adapter.cases.get(view['case_id'], owner)
        path = f"/api/investigation/cases/{view['case_id']}"
        result = client.post(path + '/cancel', json={'operation_id': 'cancel',
            'expected_revision': view['revision']})
        assert result.status_code == 200, result.text
        closed = result.json()
        assert closed['status'] == 'closing' and not closed['environment_released']
        assert closed['closure_intent']['blockers']
        assert closed['usage']['tokens'] is None
        assert read_json(adapter.cases.root / 'environment.json')['active_case'] == view['case_id']
        again = client.post(path + '/close-reconcile', json={'operation_id': 'check'})
        assert again.status_code == 200 and again.json()['status'] == 'closing'
        assert calls == []


def test_closure_auth_owner_revision_origin_and_read_only_boundaries(closure_api):
    settings, adapter, _, _, calls = closure_api
    app = create_app(settings, investigation_adapter=adapter)
    with TestClient(app, base_url='https://testserver') as first, TestClient(app, base_url='https://testserver') as other:
        login(first)
        view = new_case(first)
        path = f"/api/investigation/cases/{view['case_id']}"
        body = {'operation_id': 'cancel', 'expected_revision': view['revision']}
        assert other.post(path + '/cancel', json=body).status_code == 401
        login(other, OTHER)
        for endpoint in ('cancel', 'close-expired', 'close-reconcile'):
            data = body if endpoint == 'cancel' else {'operation_id': 'check'}
            assert other.post(path + '/' + endpoint, json=data).status_code == 404
        for extra in ('actor', 'reason', 'generation', 'policy', 'worker_id', 'product_receipt'):
            assert first.post(path + '/cancel', json={**body, extra: 'forged'}).status_code == 422
        assert first.post(path + '/cancel', json={**body, 'expected_revision': 999}).status_code == 409
        assert first.post(path + '/cancel', json=body, headers={'Origin': 'https://elsewhere.example'}).status_code == 403
        assert first.post(path + '/close-expired', json={'operation_id': 'early'}).status_code == 409
        assert first.get(path).json()['closure_intent'] is None
        approved = first.post(path + '/cancel', json=body)
        assert approved.status_code == 200 and approved.json()['status'] == 'closed'
        assert first.post(path + '/cancel', json=body).json() == approved.json()
    with TestClient(create_app(replace(settings, deployment='review'), investigation_adapter=adapter),
                    base_url='https://testserver') as review:
        login(review)
        assert review.post(path + '/cancel', json=body).status_code == 403
    assert calls == []


def test_trusted_lifespan_sweep_reconciles_without_get_or_new_worker(closure_api, monkeypatch):
    from threading import Event

    from forgeflow.api import investigation

    settings, adapter, host, clock, calls = closure_api
    view = adapter.create_case('sweep-owner', 'sweep-case')
    clock[0] = 201
    completed = Event()

    class FastTick(Event):
        def wait(self, timeout=None):
            return super().wait(min(timeout, .01) if timeout is not None else timeout)

    def sweep():
        host.expire_pending()
        completed.set()

    adapter.expire_pending = sweep
    monkeypatch.setattr(investigation, 'Event', FastTick)
    with TestClient(create_app(settings, investigation_adapter=adapter)):
        assert completed.wait(2)
        final = adapter.cases.get(view['case_id'], 'sweep-owner')
        assert final['status'] == 'closed' and final['environment_released']
        assert final['confirmation'] is None and calls == []
    monkeypatch.setattr(investigation, 'Thread', lambda **_: pytest.fail('Review must not run the sweep'))
    with TestClient(create_app(replace(settings, deployment='review'), investigation_adapter=adapter)):
        pass

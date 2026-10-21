"""v9 authorization contract using only temporary, synthetic records."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.dsh_harness import DshReply
from forgeflow.api.dsh_permissions import DISABLED_MODULES, scoped_patch
from forgeflow.api.evidence import ReadOnlyEvidenceProjection, public_evidence
from forgeflow.api.runs import RunService
from forgeflow.api.settings import create_settings
from forgeflow.product import FakeProductExecutor, ProductCaseService
from forgeflow.product.service import actor_label

CODES = ('test-evidence-owner-a', 'test-evidence-owner-b')


def settings_for(tmp_path, **extra):
    return create_settings({
        'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
        'FORGEFLOW_APPROVALS_ROOT': str(tmp_path / 'approvals'),
        'FORGEFLOW_SESSION_SECRET': 'test-evidence-secret-01234567890123456789',
        'FORGEFLOW_INVITE_CODES': ','.join(CODES),
        **extra,
    })


def login(client, index=0):
    client.cookies.clear()
    result = client.post('/api/auth/session', json={'invite_code': CODES[index]})
    assert result.status_code == 201
    return result.json()['uid']


def mcp_module(path):
    spec = importlib.util.spec_from_file_location('scoped_mcp', Path(__file__).parents[1] / 'scripts/assistant_mcp.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.EVIDENCE = str(path)
    return module


def test_approval_receipt_matches_authenticated_identity_across_api_and_mcp(tmp_path, monkeypatch):
    monkeypatch.delenv('FORGEFLOW_DSH_ENABLED', raising=False)
    settings = settings_for(tmp_path)
    executor = FakeProductExecutor()
    with TestClient(create_app(settings, product_executor=executor), base_url='https://testserver') as client:
        uid = login(client)
        principal = client.get('/api/auth/me').json()
        created = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        case_id, plan_id = created['case_id'], created['plan']['plan_id']
        comment = 'Reviewed the order and approved this exact recovery plan.'
        response = client.post(f'/api/product/cases/{case_id}/approve', json={
            'plan_id': plan_id, 'comment': comment,
            'approver': 'forged-model-identity', 'approver_label': 'Forged display name',
        })
        assert response.status_code == 200, response.text
        approved = response.json()
        receipt = approved['approval']
        assert set(receipt) == {'expires_at', 'issued_at', 'comment', 'plan_id', 'approver_label'}
        assert receipt['approver_label'] == principal['display_name'] == actor_label(uid)
        assert receipt['issued_at'] and receipt['expires_at']
        assert receipt['comment'] == comment and receipt['plan_id'] == plan_id
        assert uid not in json.dumps(approved)
        assert 'forged' not in json.dumps(approved).lower()
        assert client.get(f'/api/product/cases/{case_id}').json()['approval'] == receipt
        assert client.get('/api/product/cases').json()[0]['approval'] == receipt
        repeated = client.post(f'/api/product/cases/{case_id}/approve', json={
            'plan_id': plan_id, 'comment': 'Do not overwrite the original receipt',
        })
        assert repeated.status_code == 200 and repeated.json()['approval'] == receipt
        assert executor.apply_calls == 1

        cases = ProductCaseService(settings.approvals_root, executor)
        runs = RunService(settings.data_root, settings.approvals_root / 'runs.json')
        mcp = mcp_module(ReadOnlyEvidenceProjection(settings, cases, runs).publish(uid))
        assert mcp._call('get_case', {'case_id': case_id})['approval'] == receipt
        login(client, 1)
        assert client.get(f'/api/product/cases/{case_id}').status_code == 404


def test_approval_label_is_derived_and_private_authority_is_removed():
    uid = 'trusted-test-principal'
    value = {'approval': {'approver': uid, 'approver_label': 'Spoofed',
                          'nonce': 'single-use-authority', 'token': 'secret-token'}}
    projected = public_evidence(value)
    assert projected == {'approval': {'approver_label': actor_label(uid)}}
    assert public_evidence(projected) == projected


@pytest.mark.parametrize('question,expected,forbidden', [
    ('Did execution pass verification?', 'original ERP verifier was not run', 'original verifier reports passed'),
    ('执行验收通过了吗？', '未运行原始 ERP 验收', '原始验收结果为通过'),
])
def test_fake_executor_api_answer_labels_simulated_verification(tmp_path, monkeypatch, question, expected, forbidden):
    monkeypatch.delenv('FORGEFLOW_DSH_ENABLED', raising=False)
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        response = client.post(f"/api/product/cases/{case['case_id']}/approve", json={'plan_id': case['plan']['plan_id']})
        assert response.status_code == 200
        assert response.json()['result']['strict_success'] is True
        assert response.json()['result']['verified'] is False
        assert response.json()['result']['simulation_passed'] is True
        answer = client.post(f"/api/product/cases/{case['case_id']}/ask", json={'question': question})
        assert answer.status_code == 200
        assert expected in answer.json()['answer']
        assert forbidden not in answer.json()['answer']
        from forgeflow.api.case_context import answer_context

        assert answer.json()['case_context'] == answer_context(response.json())


def test_executor_disconnect_keeps_owner_reads_and_ask(tmp_path, monkeypatch):
    monkeypatch.delenv('FORGEFLOW_DSH_ENABLED', raising=False)
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
    with TestClient(create_app(settings), base_url='https://testserver') as client:
        login(client)
        assert [row['case_id'] for row in client.get('/api/product/cases').json()] == [case['case_id']]
        assert client.get(f"/api/product/cases/{case['case_id']}").status_code == 200
        answer = client.post(f"/api/product/cases/{case['case_id']}/ask", json={'question': 'What happened?'})
        assert answer.status_code == 200
        assert answer.json()['evidence_refs'][0]['id'] == case['case_id']
        capabilities = client.get('/api/capabilities').json()
        assert capabilities['case_read'] and capabilities['case_qa']
        assert not capabilities['case_create'] and not capabilities['case_approve']
        assert client.post(f"/api/product/cases/{case['case_id']}/approve", json={'plan_id': case['plan']['plan_id']}).status_code == 503
        login(client, 1)
        assert client.get('/api/product/cases').json() == []
        assert client.get(f"/api/product/cases/{case['case_id']}").status_code == 404
        assert client.post(f"/api/product/cases/{case['case_id']}/ask", json={'question': 'What happened?'}).status_code == 404


def test_api_and_mcp_share_host_scope_and_refresh(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    calls = []

    class Harness:
        def with_evidence(self, path):
            calls.append(path)
            class Scoped:
                def ask(self, question, key):
                    rows = mcp_module(path)._call('list_cases', {})
                    return DshReply(json.dumps(rows), [], [], None, 'project')
            return Scoped()

    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        uid_a = login(client)
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        scope_a = client.get('/api/auth/me').json()
        reply = client.post('/api/assistant/answer', json={'question': 'My cases?'}).json()
        assert json.loads(reply['answer']) == client.get('/api/product/cases').json()
        assert uid_a not in reply['answer']
        login(client, 1)
        scope_b = client.get('/api/auth/me').json()
        reply = client.post('/api/assistant/answer', json={'question': 'My cases?'}).json()
        assert json.loads(reply['answer']) == []
        assert scope_a['scope_id'] != scope_b['scope_id']
        assert calls[0] != calls[1]
        mcp = mcp_module(calls[1])
        with pytest.raises(ValueError, match='case_not_found'):
            mcp._call('get_case', {'case_id': case['case_id']})
        with pytest.raises(ValueError, match='invalid_tool_arguments'):
            mcp._call('list_cases', {'uid': uid_a})
        with pytest.raises(ValueError, match='unknown_tool'):
            mcp._call('apply', {})
        login(client)
        assert client.get('/api/auth/me').json() == scope_a
        second = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        reply = client.post('/api/assistant/answer', json={'question': 'My cases?'}).json()
        assert len(json.loads(reply['answer'])) == 2
        assert second['case_id'] in reply['answer']


def test_missing_scope_fails_closed_and_public_runs_are_labelled(tmp_path):
    settings = settings_for(tmp_path, FORGEFLOW_PUBLIC_RUNS='public-run')
    root = settings.data_root / 'public-run'
    root.mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps({'run_id': 'public-run', 'agent': 'pi', 'state': 'completed', 'task': '2280_medium_repair_plan_medium'}))
    cases = ProductCaseService(settings.approvals_root)
    runs = RunService(settings.data_root, settings.approvals_root / 'runs.json')
    projection = ReadOnlyEvidenceProjection(settings, cases, runs)
    snapshot = projection.publish('owner-a')
    mcp = mcp_module(snapshot)
    assert mcp._call('list_runs', {})[0]['access_scope'] == 'public_evidence'
    mcp.EVIDENCE = ''
    with pytest.raises(ValueError, match='evidence_scope_unavailable'):
        mcp._call('list_cases', {})
    mcp.EVIDENCE = str(snapshot)
    with pytest.raises(ValueError, match='run_not_found'):
        mcp._call('get_run', {'run_id': '../another-owner'})


def test_evidence_summary_preserves_comparison_scope_without_raw_manifest(tmp_path):
    settings = settings_for(tmp_path, FORGEFLOW_PUBLIC_RUNS='public-run')
    root = settings.data_root / 'public-run'
    root.mkdir(parents=True)
    (root / 'manifest.json').write_text(json.dumps({
        'run_id': 'public-run', 'agent': 'nop', 'state': 'completed',
        'task': '2280_medium_repair_plan_medium',
        'started_at_utc': '2026-10-22T12:00:00Z',
        'model': {'model': 'test-model', 'credential': 'never-publish-this'},
        'budget': {'agent_timeout_sec': 600},
        'private_notes': 'never-publish-this',
    }))
    cases = ProductCaseService(settings.approvals_root)
    runs = RunService(settings.data_root, settings.approvals_root / 'runs.json')
    projection = ReadOnlyEvidenceProjection(settings, cases, runs)
    summary = projection.list_runs('owner-a')[0]
    detail = projection.get_run('public-run', 'owner-a')
    assert summary['started_at'] == '2026-10-22T12:00:00Z'
    assert summary['agent']['category'] == 'environment_control'
    assert summary['experiment'] == detail['experiment']
    assert summary['experiment']['model'] == 'test-model'
    assert 'never-publish-this' not in json.dumps(summary)


def test_scoped_composition_disables_tools_and_binds_exact_snapshot(tmp_path):
    patch = tmp_path / 'base.json'
    patch.write_text(json.dumps([{'insert': [{'id': 'mcp-forgeflow', 'name': '@deepseek-ai/dsh-mcp-client', 'config': {'serverName': 'forgeflow', 'transport': 'stdio', 'env': {'FORGEFLOW_ASSISTANT_CASES': 'forbidden'}}}]}]))
    evidence_a, evidence_b = tmp_path / 'a.json', tmp_path / 'b.json'
    a = json.loads(scoped_patch(patch, evidence_a).read_text())
    b = json.loads(scoped_patch(patch, evidence_b).read_text())
    assert a[0]['insert'][0]['config']['env'] == {'FORGEFLOW_ASSISTANT_EVIDENCE': str(evidence_a)}
    assert b[0]['insert'][0]['config']['env'] == {'FORGEFLOW_ASSISTANT_EVIDENCE': str(evidence_b)}
    assert {row['id'] for row in a[1:] if row.get('disabled')} == set(DISABLED_MODULES)
    stale = json.loads(patch.read_text()) + [{'id': tool, 'disabled': False} for tool in DISABLED_MODULES]
    stale.append({'id': 'session-telemetry-otel', 'config': {'mode': 'FEEDBACK_ONLY'}})
    patch.write_text(json.dumps(stale))
    refreshed = json.loads(scoped_patch(patch, evidence_a).read_text())
    effective = {row['id']: row for row in refreshed if 'id' in row}
    assert all(effective[tool]['disabled'] is True for tool in DISABLED_MODULES)
    assert effective['session-telemetry-otel']['config']['mode'] == 'DISABLED'
    patch.write_text('[{"id":"unknown-plugin"}]')
    with pytest.raises(ValueError, match='unreviewed'):
        scoped_patch(patch, evidence_a)


def test_native_and_global_sessions_are_not_exposed_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv('FORGEFLOW_DEVELOPER_TOOLS', raising=False)
    monkeypatch.setenv('FORGEFLOW_DSH_WEB_URL', 'http://localhost/?token=test-only')
    monkeypatch.delenv('FORGEFLOW_DSH_ENABLED', raising=False)
    with TestClient(create_app(settings_for(tmp_path)), base_url='https://testserver') as client:
        login(client)
        assert client.get('/api/config').json()['dsh_web_url'] == ''
        assert client.get('/api/sessions').json() == []


def test_executor_health_is_read_only_and_distinct_from_erp_readiness(tmp_path, monkeypatch):
    monkeypatch.delenv('FORGEFLOW_DSH_ENABLED', raising=False)
    calls = []
    class LiveConfigured:
        live = True
        socket_path = str(tmp_path / 'synthetic-test.sock')
        def prepare(self, _):
            raise AssertionError('health must not prepare')
        def apply(self, *_):
            raise AssertionError('health must not apply')

    connected = False
    def health(path, *, timeout):
        calls.append((path, timeout))
        return {'ok': connected, 'pid': 1, 'socket': 'must-not-leak'}
    monkeypatch.setattr('forgeflow.recovery.client.health', health)
    with TestClient(create_app(settings_for(tmp_path), product_executor=LiveConfigured()), base_url='https://testserver') as client:
        login(client)
        offline = client.get('/api/capabilities').json()
        assert offline['environment']['configured'] is True
        assert offline['environment']['connection'] == 'unavailable'
        assert offline['case_create'] is False
        connected = True
        online = client.get('/api/capabilities').json()
        assert online['case_create'] is True
        assert online['environment']['connection'] == 'connected'
        assert online['environment']['erp_ready'] is None
        assert online['environment']['checked_at']
        assert 'must-not-leak' not in json.dumps(online)
        assert client.delete('/api/auth/session').status_code == 204
        assert client.get('/api/auth/me').status_code == 401
    assert len(calls) == 2


@pytest.mark.parametrize('strict,valid,failed,phrase', [
    (None, None, 0, 'verification remains unknown'),
    (True, False, 0, 'verification remains unknown'),
    (True, True, 1, 'verifier reports failed'),
    (False, True, 1, 'verifier reports failed'),
    (True, True, 0, 'verifier reports passed'),
])
def test_case_answers_require_strict_original_verifier_evidence(strict, valid, failed, phrase):
    from forgeflow.api.case_qa import answer_case
    case = {'state': 'succeeded', 'execution': {'status': 'succeeded', 'writes': [],
        'readback': {'execution_validated': True},
        'verifier': {'status': 'passed', 'score': 100, 'strict_success': strict,
                     'evaluation_valid': valid, 'failed_rule_count': failed,
                     'evaluation_scope': 'product_original_verifier'}}}
    answer = answer_case('Did execution pass verification?', case)['answer']
    assert phrase in answer
    if strict is not True or valid is not True or failed:
        assert 'verifier reports passed' not in answer


def test_verifier_check_counts_do_not_present_applicable_as_passed():
    from forgeflow.api.case_qa import answer_case
    verifier = {'status': 'failed', 'strict_success': False, 'evaluation_valid': True,
                'evaluation_scope': 'product_original_verifier',
                'checks': [{'applicable': 5, 'total': 6}]}
    case = {'state': 'failed', 'execution': {'status': 'succeeded', 'writes': [],
        'readback': {'execution_validated': True}, 'verifier': verifier}}
    answer = answer_case('Did execution pass verification?', case)['answer']
    assert '5 applicable, 6 total' in answer
    assert '5 passed' not in answer
    verifier.update(passed_rule_count=4, applicable_rule_count=5)
    answer = answer_case('Did execution pass verification?', case)['answer']
    assert '4 passed, 5 applicable, 6 total' in answer
    verifier.pop('evaluation_scope')
    answer = answer_case('Did execution pass verification?', case)['answer']
    assert 'evidence source is unconfirmed' in answer
    assert 'original verifier reports' not in answer


def test_removed_ownership_retires_old_snapshot_and_session(tmp_path, monkeypatch):
    paths, closed = [], []
    class Harness:
        def with_evidence(self, path):
            paths.append(path)
            class Scoped:
                def ask(self, question, key):
                    rows = mcp_module(path)._call('list_cases', {})
                    return DshReply(json.dumps(rows), [], [], None, 'project')
                def close(self):
                    closed.append(path)
            return Scoped()
    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        assert case['case_id'] in client.post('/api/assistant/answer', json={'question': 'My cases?'}).json()['answer']
        record_path = settings.approvals_root / 'product-cases' / f"{case['case_id']}.json"
        record = json.loads(record_path.read_text())
        record['uid'] = 'new-owner'
        record_path.write_text(json.dumps(record))
        result = client.post('/api/assistant/answer', json={'question': 'My cases?'}).json()
        assert result['answer'] == '[]'
        assert paths[0] != paths[1] and closed == [paths[0]]
        assert not paths[0].exists()


def test_scope_change_cannot_replay_old_sse_events(tmp_path, monkeypatch):
    class Harness:
        def with_evidence(self, path):
            class Scoped:
                def stream(self, question, key):
                    rows = mcp_module(path)._call('list_cases', {})
                    yield {'type': 'final', 'answer': json.dumps(rows)}
            return Scoped()
    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        request = {'question': 'My cases?', 'conversation_id': 'review'}
        initial = client.post('/api/assistant/stream', json=request)
        assert case['case_id'] in initial.text
        path = settings.approvals_root / 'product-cases' / f"{case['case_id']}.json"
        record = json.loads(path.read_text())
        record['uid'] = 'another-owner'
        path.write_text(json.dumps(record))
        resumed = client.post('/api/assistant/stream', json=request, headers={'Last-Event-ID': '99'})
        assert resumed.status_code == 200
        assert case['case_id'] not in resumed.text
        assert '"answer": "[]"' in resumed.text


def test_model_response_is_discarded_if_ownership_changes_during_turn(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    class Harness:
        def with_evidence(self, path):
            class Scoped:
                def ask(self, question, key):
                    case = mcp_module(path)._call('list_cases', {})[0]
                    record_path = settings.approvals_root / 'product-cases' / f"{case['case_id']}.json"
                    record = json.loads(record_path.read_text())
                    record['uid'] = 'another-owner'
                    record_path.write_text(json.dumps(record))
                    return DshReply('private stale answer', [], [], None, 'project')
            return Scoped()
    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        client.post('/api/product/cases', json={'scenario': 'A'})
        result = client.post('/api/assistant/answer', json={'question': 'My cases?'})
        assert result.status_code == 409
        assert result.json()['detail'] == 'evidence_scope_changed'
        assert 'private stale answer' not in result.text


def test_case_decision_matches_disconnected_capability_and_review(tmp_path, monkeypatch):
    monkeypatch.delenv('FORGEFLOW_DSH_ENABLED', raising=False)
    settings = settings_for(tmp_path)
    with TestClient(create_app(settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
    class LiveUnavailable:
        live = True
        socket_path = str(tmp_path / 'not-a-real-socket')
    monkeypatch.setattr('forgeflow.recovery.client.health', lambda *args, **kwargs: {'ok': False})
    with TestClient(create_app(settings, product_executor=LiveUnavailable()), base_url='https://testserver') as client:
        login(client)
        capabilities = client.get('/api/capabilities').json()
        detail = client.get(f"/api/product/cases/{case['case_id']}").json()
        assert not capabilities['case_approve'] and not detail['decision']['can_approve']
        assert not detail['decision']['can_select_candidate']
        assert not detail['decision']['can_resume_selection']
        assert detail['decision']['reason'] == 'product_execution_unavailable'
    review_settings = settings_for(tmp_path, FORGEFLOW_DEPLOYMENT='review')
    with TestClient(create_app(review_settings, product_executor=FakeProductExecutor()), base_url='https://testserver') as client:
        login(client)
        detail = client.get(f"/api/product/cases/{case['case_id']}").json()
        assert not any(detail['decision'][key] for key in ('can_approve', 'can_reject', 'can_request_changes',
                                                         'can_select_candidate', 'can_resume_selection'))
        assert detail['decision']['reason'] == 'review_read_only'

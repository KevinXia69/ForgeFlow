"""Owner-bound turn recovery with blocking synthetic producers, no runtime data."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.settings import create_settings
from forgeflow.product import FakeProductExecutor


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


@pytest.fixture
def streaming_app(tmp_path, monkeypatch):
    calls = []
    started = threading.Event()
    release = threading.Event()

    class Harness:
        def with_evidence(self, _path):
            class Scoped:
                supports_turn_cancellation = True

                def stream(self, question, key, *, cancel_event=None):
                    calls.append((question, key))
                    started.set()
                    while not release.wait(.01):
                        if cancel_event is not None and cancel_event.is_set():
                            return
                    yield {'type': 'trace', 'activity': []}
                    yield {'type': 'final', 'answer': question, 'mode': 'harness', 'citations': [],
                           'case_context': {'case_id': 'forged-provider-context'}}
            return Scoped()

    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())
    settings = create_settings({
        'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
        'FORGEFLOW_APPROVALS_ROOT': str(tmp_path / 'state'),
        'FORGEFLOW_SESSION_SECRET': 'synthetic-turn-secret-01234567890123456789',
        'FORGEFLOW_INVITE_CODES': 'synthetic-turn-owner-a,synthetic-turn-owner-b',
        'FORGEFLOW_SECURE_COOKIE': 'false',
    })
    with TestClient(create_app(settings, product_executor=FakeProductExecutor())) as client:
        assert client.post('/api/auth/session', json={'invite_code': 'synthetic-turn-owner-a'}).status_code == 201
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()
        yield client, calls, started, release, case['case_id']
        release.set()


def routes(kind, case_id):
    prefix = '/api/assistant' if kind == 'knowledge' else f'/api/product/cases/{case_id}/ask'
    return prefix + '/stream', prefix + '/cancel'


@pytest.mark.parametrize('kind', ['knowledge', 'case'])
def test_stop_then_new_question_never_reuses_old_answer(streaming_app, kind):
    client, calls, started, release, case_id = streaming_app
    stream, cancel = routes(kind, case_id)
    first = {'question': 'first question', 'conversation_id': 'chat', 'turn_id': 'first'}
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, stream, json=first)
        assert started.wait(2)
        assert client.post(cancel, json={'conversation_id': 'chat', 'turn_id': 'first'}).json() == {
            'cancelled': True, 'provider_interrupt_confirmed': False}
        stopped = pending.result(timeout=3)
        assert not any(event.get('type') == 'final' for event in events(stopped))
    assert client.post(stream, json={**first, 'resume': True}).status_code == 409
    release.set()
    second = client.post(stream, json={**first, 'turn_id': 'second', 'question': 'second question'})
    final = events(second)[-1]
    assert final['type'] == 'final' and 'second question' in final['answer']
    assert 'first question' not in final['answer']
    assert len(calls) == 2
    assert calls[0][1] == calls[1][1]  # Conversation memory survives distinct turn IDs.
    if kind == 'case':
        assert final['evidence_refs'][0]['id'] == case_id
        assert final['case_context']['case_id'] == case_id


@pytest.mark.parametrize('kind', ['knowledge', 'case'])
def test_resume_before_first_frame_and_completed_replay_make_no_new_call(streaming_app, kind):
    client, calls, started, release, case_id = streaming_app
    stream, _ = routes(kind, case_id)
    body = {'question': 'remember this turn', 'conversation_id': 'chat', 'turn_id': 'turn-one'}
    with ThreadPoolExecutor() as pool:
        original = pool.submit(client.post, stream, json=body)
        assert started.wait(2)
        resumed = pool.submit(client.post, stream, json={**body, 'resume': True})
        release.set()
        assert events(original.result(timeout=3))[-1]['type'] == 'final'
        assert events(resumed.result(timeout=3))[-1]['type'] == 'final'
    assert len(calls) == 1
    assert client.post(stream, json={**body, 'resume': True}).status_code == 200
    assert client.post(stream, json=body).status_code == 200
    assert len(calls) == 1
    assert client.post(stream, json={**body, 'question': 'different question'}).status_code == 409
    assert client.post(stream, json={**body, 'turn_id': 'missing', 'resume': True}).status_code == 410
    assert len(calls) == 1


@pytest.mark.parametrize('kind', ['knowledge', 'case'])
def test_early_cancel_and_other_owner_cannot_recover_or_restart_turn(streaming_app, kind):
    client, calls, _, release, case_id = streaming_app
    stream, cancel = routes(kind, case_id)
    body = {'question': 'protected question', 'conversation_id': 'chat', 'turn_id': 'early'}
    assert client.post(cancel, json={'conversation_id': 'chat', 'turn_id': 'early'}).status_code == 200
    assert client.post(stream, json=body).status_code == 409
    assert calls == []
    release.set()
    body['turn_id'] = 'ready'
    assert client.post(stream, json=body).status_code == 200
    client.cookies.clear()
    assert client.post('/api/auth/session', json={'invite_code': 'synthetic-turn-owner-b'}).status_code == 201
    denied = client.post(stream, json={**body, 'resume': True})
    assert denied.status_code == (410 if kind == 'knowledge' else 404)
    assert len(calls) == 1


def test_case_answer_retains_original_state_after_progress_and_stream_replay(streaming_app):
    from forgeflow.api.case_context import answer_context

    client, calls, started, release, case_id = streaming_app
    route, _ = routes('case', case_id)
    before = client.get(f'/api/product/cases/{case_id}').json()
    body = {'question': 'Explain the current plan', 'conversation_id': 'history-context', 'turn_id': 'original'}
    with ThreadPoolExecutor() as pool:
        response = pool.submit(client.post, route, json=body)
        assert started.wait(2)
        advanced = client.post(f'/api/product/cases/{case_id}/approve',
                               json={'plan_id': before['plan']['plan_id']})
        assert advanced.status_code == 200
        assert advanced.json()['state'] == 'succeeded'
        release.set()
        final = events(response.result(timeout=3))[-1]
    assert final['case_context'] == answer_context(before)
    assert final['case_context']['state'] == 'awaiting_approval'
    replay = events(client.post(route, json={**body, 'resume': True}))[-1]
    assert replay['case_context'] == final['case_context']
    assert len(calls) == 1


def test_completed_turn_survives_a_restart_at_the_api_level(tmp_path, monkeypatch):
    """A restart must not turn the same question into a 409.

    The journal used to drop the request digest, so a restored turn carried no
    hash, the endpoint's payload check failed, and every retry of the same
    question came back 409 turn_payload_mismatch. This drives the HTTP endpoint
    across two app instances, not the hub alone.
    """
    import json as _json

    calls: list[str] = []

    class Harness:
        def with_evidence(self, _path):
            class Scoped:
                supports_turn_cancellation = True

                def stream(self, question, _key, *, cancel_event=None):
                    calls.append(question)
                    yield {'type': 'final', 'answer': f'answer to {question}', 'mode': 'harness',
                           'citations': [], 'case_context': {'case_id': 'restart'}}
            return Scoped()

    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())

    def app():
        settings = create_settings({
            'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
            'FORGEFLOW_APPROVALS_ROOT': str(tmp_path / 'state'),
            'FORGEFLOW_SESSION_SECRET': 'synthetic-turn-secret-01234567890123456789',
            'FORGEFLOW_INVITE_CODES': 'synthetic-turn-owner-a',
            'FORGEFLOW_SECURE_COOKIE': 'false',
        })
        return create_app(settings, product_executor=FakeProductExecutor())

    body = {'question': 'what is waiting on me', 'turn_id': 'turn-restart'}

    with TestClient(app()) as client:
        assert client.post('/api/auth/session', json={'invite_code': 'synthetic-turn-owner-a'}).status_code == 201
        first = client.post('/api/assistant/stream', json=body)
        assert first.status_code == 200
        assert calls == ['what is waiting on me']

    with TestClient(app()) as client:
        assert client.post('/api/auth/session', json={'invite_code': 'synthetic-turn-owner-a'}).status_code == 201
        resumed = client.post('/api/assistant/stream', json={**body, 'resume': True})
        assert resumed.status_code == 200, resumed.text
        assert 'answer to what is waiting on me' in resumed.text
        assert calls == ['what is waiting on me'], 'the restart re-ran the model'
        changed = client.post('/api/assistant/stream', json={**body, 'question': 'a different question'})
        assert changed.status_code == 409
        assert changed.json()['detail'] == 'turn_payload_mismatch'

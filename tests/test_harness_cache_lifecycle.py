"""Cache pressure must not close a selected or not-yet-started runtime."""
from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from turn_helpers import turn_id

from forgeflow.api.app import create_app
from forgeflow.api.dsh_harness import DshReply
from forgeflow.api.evidence import ReadOnlyEvidenceProjection
from forgeflow.api.settings import create_settings
from forgeflow.api.stream_hub import StreamJob
from forgeflow.product import FakeProductExecutor


@pytest.fixture
def cache_app(tmp_path, monkeypatch):
    created = []
    revision = [0]
    snapshot = ReadOnlyEvidenceProjection.snapshot

    def versioned(self, uid):
        result = snapshot(self, uid)
        result['test_content_revision'] = revision[0]
        return result

    class Scoped:
        closed = False
        supports_turn_cancellation = True

        def ask(self, question, key):
            assert not self.closed
            return DshReply('A bounded answer.', [], [], None, 'project')

        def stream(self, question, key, *, cancel_event=None):
            assert not self.closed
            assert not cancel_event.is_set(), 'cancelled before dispatch'
            yield {'type': 'final', 'answer': 'A bounded answer.'}

        def close(self):
            self.closed = True

    class Harness:
        def with_evidence(self, path):
            result = Scoped()
            created.append(result)
            return result

    monkeypatch.setattr(ReadOnlyEvidenceProjection, 'snapshot', versioned)
    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: Harness())
    settings = create_settings({
        'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
        'FORGEFLOW_APPROVALS_ROOT': str(tmp_path / 'state'),
        'FORGEFLOW_SESSION_SECRET': 'synthetic-cache-secret-01234567890123456789',
        'FORGEFLOW_INVITE_CODES': 'synthetic-cache-owner',
        'FORGEFLOW_SECURE_COOKIE': 'false',
    })
    with TestClient(create_app(settings, product_executor=FakeProductExecutor())) as client:
        assert client.post('/api/auth/session', json={'invite_code': 'synthetic-cache-owner'}).status_code == 201
        case = client.post('/api/product/cases', json={'scenario': 'A'}).json()

        def pressure(start):
            for number in range(start, start + 33):
                revision[0] = number
                assert client.post('/api/assistant/answer', json={'question': 'cache pressure'}).status_code == 200
            assert sum(not item.closed for item in created) <= 32

        yield client, case['case_id'], created, pressure


@pytest.mark.parametrize('outcome', ['success', 'exception'])
def test_selected_harness_is_reserved_and_route_exits_release_it(cache_app, monkeypatch, outcome):
    client, case_id, created, pressure = cache_app
    entered, release = threading.Event(), threading.Event()
    original = ReadOnlyEvidenceProjection.snapshot_case

    def delayed(path, wanted):
        entered.set()
        assert release.wait(5)
        if outcome == 'exception':
            raise HTTPException(status_code=409, detail='synthetic_failure')
        return original(path, wanted)

    monkeypatch.setattr(ReadOnlyEvidenceProjection, 'snapshot_case', staticmethod(delayed))
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, f'/api/product/cases/{case_id}/ask', json={'question': 'case facts'})
        assert entered.wait(2)
        try:
            pressure(1)
            assert not created[0].closed
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == (200 if outcome == 'success' else 409)
    pressure(40)
    assert created[0].closed, 'route reservation leaked after exit'


def test_not_started_stream_keeps_reservation_then_cancel_releases_it(cache_app, monkeypatch):
    client, _, created, pressure = cache_app
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = StreamJob._run

    def delayed(job):
        entered.set()
        assert release.wait(5)
        try:
            original(job)
        finally:
            finished.set()

    monkeypatch.setattr(StreamJob, '_run', delayed)
    body = {'question': 'queued stream', 'conversation_id': 'queued', 'turn_id': turn_id('cancel-before-start')}
    with ThreadPoolExecutor() as pool:
        pending = pool.submit(client.post, '/api/assistant/stream', json=body)
        assert entered.wait(2)
        try:
            pressure(1)
            assert not created[0].closed
            assert client.post('/api/assistant/cancel', json={
                'conversation_id': 'queued', 'turn_id': turn_id('cancel-before-start'),
            }).json()['cancelled']
        finally:
            release.set()
        assert pending.result(timeout=3).status_code == 200
        assert finished.wait(2)
    pressure(40)
    assert created[0].closed, 'unstarted cancellation retained its runtime reservation'


def test_cancel_after_trace_closes_cached_producer():
    closed = threading.Event()
    job = None

    def producer():
        try:
            yield {'type': 'trace', 'activity': []}
            job.cancel()
            yield {'type': 'trace', 'activity': []}
        finally:
            closed.set()

    job = StreamJob('synthetic', producer())
    job._run()
    assert job.cancelled and job.done
    assert closed.is_set(), 'cached cancelled generator kept runtime/budget resources open'

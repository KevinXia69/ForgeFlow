"""Real PostgreSQL authorization/version/replay acceptance for reference facts.

Only an explicitly disposable database is accepted. Each case owns one schema;
the provider is synthetic, but catalog, bindings, ACL and SSE journal are real.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import psycopg
import pytest
from fastapi.testclient import TestClient
from psycopg import sql
from psycopg.conninfo import make_conninfo

from forgeflow.api.app import create_app
from forgeflow.api.auth import issue_cookie, verify_cookie
from forgeflow.api.dsh_harness import DshReply
from forgeflow.api.settings import create_settings
from forgeflow.knowledge.catalog import KnowledgeCatalog
from forgeflow.knowledge.documents_catalog import DocumentCatalog
from forgeflow.knowledge.reference_pack import load_reference_pack
from forgeflow.product import FakeProductExecutor
from forgeflow.superagent.bindings import bind_odoo_user
from forgeflow.superagent.store import ControlStore

DATABASE_URL = os.environ.get('FORGEFLOW_REFERENCE_FACTS_TEST_DATABASE_URL', '')
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason='dedicated reference facts test DB not set')
QUESTION = 'MY2N-GS-R DC24 带 LED，线圈参数如何核对？'
PACK = Path(__file__).resolve().parents[1] / 'knowledge/reference/supply-recovery-v1'


def events(response):
    return [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith('data: ')]


@pytest.fixture
def facts(tmp_path, monkeypatch):
    schema = 'facts_test_' + uuid4().hex
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        database = admin.execute('SELECT current_database()').fetchone()[0]
        assert database.startswith(('forgeflow_facts_test_', 'forgeflow_admission_test_'))
        admin.execute(sql.SQL('CREATE SCHEMA {}').format(sql.Identifier(schema)))
        monkeypatch.setenv('FORGEFLOW_CONTROL_DATABASE_URL',
                           make_conninfo(DATABASE_URL, options=f'-c search_path={schema}'))
        monkeypatch.delenv('FORGEFLOW_ACCOUNTS_DB', raising=False)
        store = ControlStore(os.environ['FORGEFLOW_CONTROL_DATABASE_URL'])
        calls = []

        class Harness:
            def with_evidence(self, path):
                class Scoped:
                    workspace = tmp_path
                    image_root = tmp_path / 'uploads' / 'synthetic-owner'

                    def ask(self, question, key):
                        calls.append(question)
                        return DshReply('Synthetic provider fallback.', [], [], None, 'project')

                    def stream(self, question, key):
                        calls.append(question)
                        yield {'type': 'final', 'mode': 'harness',
                               'answer': 'Synthetic provider fallback.', 'citations': [], 'passages': []}
                return Scoped()

        monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', Harness)
        settings = create_settings({
            'FORGEFLOW_DATA_ROOT': str(tmp_path / 'runs'),
            'FORGEFLOW_APPROVALS_ROOT': str(tmp_path / 'state'),
            'FORGEFLOW_SESSION_SECRET': 'synthetic-facts-secret-01234567890123456789',
            'FORGEFLOW_INVITE_CODES': 'synthetic-facts-a,synthetic-facts-b',
            'FORGEFLOW_SECURE_COOKIE': 'false',
        })
        uid = verify_cookie(settings, issue_cookie(settings, 'synthetic-facts-a'))
        try:
            store.migrate()
            store.seed_tenant('facts-tenant', 'Synthetic facts tenant')
            store.seed_tenant('other-tenant', 'Other synthetic tenant')
            bind_odoo_user(store, principal_id=uid, tenant_id='facts-tenant', odoo_user_id=2)
            pack = load_reference_pack(PACK)
            catalog = DocumentCatalog(store)
            generation = KnowledgeCatalog(store).new_generation('facts-tenant')
            for document in pack.documents:
                if document.source_id not in {'K04', 'K06'}:
                    continue
                catalog.upsert(tenant_id='facts-tenant', source_path=document.source_path,
                               parsed=document.parsed, raw=document.raw)
                catalog.mark_indexed(tenant_id='facts-tenant', source_path=document.source_path,
                                     generation=generation)
            KnowledgeCatalog(store).publish('facts-tenant', generation)
            app = create_app(settings, product_executor=FakeProductExecutor())
            with TestClient(app) as client:
                assert client.post('/api/auth/session', json={'invite_code': 'synthetic-facts-a'}).status_code == 201
                yield SimpleNamespace(client=client, store=store, calls=calls, uid=uid,
                                      app=app, settings=settings, generation=generation)
        finally:
            store.close()
            admin.execute(sql.SQL('DROP SCHEMA {} CASCADE').format(sql.Identifier(schema)))


def body(**extra):
    return {'question': QUESTION, 'conversation_id': 'facts-chat', 'turn_id': 'facts-turn', **extra}


def change(facts, statement, params=()):
    with facts.store.transaction() as cursor:
        cursor.execute(statement, params or None)


@pytest.mark.parametrize('endpoint', ['answer', 'stream'])
def test_current_authorized_versions_render_two_sources_without_provider(facts, endpoint):
    from forgeflow.api.reference_facts import load_reference_snapshot
    from forgeflow.knowledge.reference_facts import plan_reference_facts, render_reference_facts
    plan = plan_reference_facts(QUESTION)
    snapshot = load_reference_snapshot(facts.store, plan, facts.uid)
    assert snapshot is not None
    assert render_reference_facts(plan, snapshot.sources)['mode'] == 'reference_facts'
    response = facts.client.post('/api/assistant/' + endpoint, json=body())
    assert response.status_code == 200
    answer = response.json() if endpoint == 'answer' else events(response)[-1]
    assert answer['mode'] == 'reference_facts'
    assert '非本次模型自由生成' in answer['answer']
    assert len(answer['citations']) == len(answer['passages']) == 2
    assert {p['path'].rsplit('/', 1)[-1] for p in answer['passages']} == {'K04.md', 'K06.md'}
    assert all(p['document_version'] == 1 and p['source_metadata'] for p in answer['passages'])
    assert facts.calls == []


@pytest.mark.parametrize('mutation', [
    "UPDATE knowledge_documents SET status='withdrawn', withdrawn_at=now() WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_documents SET review_state='rejected' WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_documents SET review_state='required' WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_documents SET parse_status='partial' WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_documents SET sha256=repeat('0',64) WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_documents SET content=content || 'tampered' WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_document_blocks SET text=text || 'tampered' WHERE ord=1",
    "UPDATE knowledge_documents SET version=version+1 WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_documents SET published_generation=NULL WHERE source_path LIKE '%K04.md'",
    "UPDATE knowledge_generations SET state='retired'",
    "DELETE FROM knowledge_role_scopes WHERE tenant_id='facts-tenant'",
    "DELETE FROM control_odoo_bindings",
    "UPDATE control_odoo_bindings SET tenant_id='other-tenant'",
])
def test_unusable_source_or_scope_never_bypasses_catalog(facts, mutation):
    change(facts, mutation)
    result = facts.client.post('/api/assistant/answer', json=body())
    assert result.status_code == 200
    assert result.json()['mode'] == 'harness'
    assert len(facts.calls) == 1


@pytest.mark.parametrize('extra', [
    {'attachment_text': 'Synthetic attached context'},
    {'history': [{'role': 'user', 'content': 'Use a different model from the prior conversation'}]},
    {'source_lang': 'en'},
    {'image': {'mime_type': 'image/png', 'data': base64.b64encode(b'\x89PNG\r\n\x1a\nsynthetic').decode()}},
    {'question': 'MY2N-GS-R DC24X 线圈参数如何核对？'},
    {'question': 'MY2N-GS-R DC24 和 MY4N-GS-R DC24 线圈参数比较'},
    {'question': 'MY2N-GS-R DC24 参数核对并批准采购'},
])
def test_other_contexts_preserve_provider_path(facts, extra):
    result = facts.client.post('/api/assistant/stream', json=body(**extra))
    assert result.status_code == 200
    assert events(result)[-1]['mode'] == 'harness'
    assert len(facts.calls) == 1


def test_attachment_owner_checks_are_not_skipped(facts):
    result = facts.client.post('/api/assistant/answer', json=body(attachments=[{
        'digest': '0' * 64, 'name': 'unowned.txt', 'media_type': 'text/plain', 'size': 1}]))
    assert result.status_code == 404
    assert facts.calls == []


@pytest.mark.parametrize('mutation', [
    "UPDATE knowledge_documents SET review_state='rejected'",
    "UPDATE knowledge_documents SET sha256=repeat('0',64)",
    "UPDATE knowledge_documents SET version=version+1",
    "UPDATE knowledge_generations SET state='retired'",
    "DELETE FROM knowledge_role_scopes WHERE tenant_id='facts-tenant'",
    "DELETE FROM control_odoo_bindings",
])
def test_completed_replay_requires_current_sources_and_authority(facts, mutation):
    assert facts.client.post('/api/assistant/stream', json=body()).status_code == 200
    assert facts.client.post('/api/assistant/stream', json=body(resume=True)).status_code == 200
    change(facts, mutation)
    response = facts.client.post('/api/assistant/stream', json=body(resume=True))
    assert response.status_code == 409
    assert 'reference_scope_changed' in response.text
    assert '37.7' not in response.text
    assert facts.calls == []


def test_payload_owner_cancel_and_journal_restart(facts):
    assert facts.client.post('/api/assistant/stream', json=body()).status_code == 200
    assert facts.client.post('/api/assistant/stream', json=body(question=QUESTION + ' please')).status_code == 409
    # Real on-disk journal, fresh API/hub, same owner and still-current sources.
    with TestClient(create_app(facts.settings, product_executor=FakeProductExecutor())) as restarted:
        assert restarted.post('/api/auth/session', json={'invite_code': 'synthetic-facts-a'}).status_code == 201
        assert events(restarted.post('/api/assistant/stream', json=body(resume=True)))[-1]['mode'] == 'reference_facts'
        assert restarted.post('/api/assistant/cancel', json={'conversation_id': 'facts-chat', 'turn_id': 'facts-turn'}).json()['cancelled']
        assert restarted.post('/api/assistant/stream', json=body(resume=True)).status_code == 409
    facts.client.cookies.clear()
    assert facts.client.post('/api/auth/session', json={'invite_code': 'synthetic-facts-b'}).status_code == 201
    assert facts.client.post('/api/assistant/stream', json=body(resume=True)).status_code == 410
    assert facts.calls == []


def test_new_publication_invalidates_old_fingerprint_even_with_identical_bytes(facts):
    assert facts.client.post('/api/assistant/stream', json=body()).status_code == 200
    catalog = KnowledgeCatalog(facts.store)
    next_generation = catalog.new_generation('facts-tenant')
    change(facts, 'UPDATE knowledge_documents SET published_generation=%s,indexed_generation=%s',
           (next_generation, next_generation))
    catalog.publish('facts-tenant', next_generation)
    assert facts.client.post('/api/assistant/stream', json=body(resume=True)).status_code == 409
    fresh = facts.client.post('/api/assistant/stream', json=body(turn_id='new-publication'))
    assert events(fresh)[-1]['mode'] == 'reference_facts'
    assert facts.calls == []


def test_restored_identical_document_gets_current_version_not_sidecar_version(facts):
    assert facts.client.post('/api/assistant/stream', json=body()).status_code == 200
    catalog = DocumentCatalog(facts.store)
    source = 'reference/supply-recovery-v1/K04.md'
    restored = catalog.restore_version(tenant_id='facts-tenant', source_path=source, version=1)
    assert restored.version == 2
    catalog.mark_indexed(tenant_id='facts-tenant', source_path=source,
                         generation=facts.generation, document_id=restored.id, version=2)
    assert facts.client.post('/api/assistant/stream', json=body(resume=True)).status_code == 409
    fresh = facts.client.post('/api/assistant/answer', json=body()).json()
    assert fresh['mode'] == 'reference_facts'
    assert {p['path'].rsplit('/', 1)[-1]: p['document_version'] for p in fresh['passages']} == {
        'K04.md': 2, 'K06.md': 1}
    assert facts.calls == []


def test_unauthenticated_request_cannot_read_pack(facts):
    facts.client.cookies.clear()
    for endpoint in ['answer', 'stream']:
        assert facts.client.post('/api/assistant/' + endpoint, json=body()).status_code == 401
    assert facts.calls == []


def test_revocation_after_subscribe_before_final_cannot_leak_cached_facts(facts, monkeypatch):
    from forgeflow.api.stream_hub import StreamJob
    original = StreamJob.subscribe

    def revoke_then_subscribe(job, last=None):
        if job.metadata.get('kind') == 'reference_facts':
            change(facts, "UPDATE knowledge_documents SET review_state='rejected'")
        yield from original(job, last)

    monkeypatch.setattr(StreamJob, 'subscribe', revoke_then_subscribe)
    result = facts.client.post('/api/assistant/stream', json=body())
    assert result.status_code == 200
    assert events(result) == [{'type': 'error', 'code': 'reference_scope_changed'}]
    assert facts.calls == []


def test_facts_without_model_support_early_stop_and_new_turn(facts, monkeypatch):
    monkeypatch.setattr('forgeflow.api.app.DshHarness.from_env', lambda: None)
    with TestClient(create_app(facts.settings, product_executor=FakeProductExecutor())) as client:
        assert client.post('/api/auth/session', json={'invite_code': 'synthetic-facts-a'}).status_code == 201
        assert client.post('/api/assistant/cancel', json={
            'conversation_id': 'facts-chat', 'turn_id': 'early'}).json()['cancelled']
        assert client.post('/api/assistant/stream', json=body(turn_id='early')).status_code == 409
        answer = events(client.post('/api/assistant/stream', json=body(turn_id='new')))[-1]
        assert answer['mode'] == 'reference_facts'
        assert client.post('/api/assistant/answer', json=body()).json()['mode'] == 'reference_facts'
    assert facts.calls == []


def test_current_account_role_overrides_legacy_binding_role(facts):
    from forgeflow.api.reference_facts import load_reference_snapshot
    from forgeflow.knowledge.reference_facts import plan_reference_facts
    change(facts, "UPDATE control_odoo_bindings SET role='admin'")
    change(facts, "UPDATE knowledge_documents SET acl_scope='engineering'")
    plan = plan_reference_facts(QUESTION)
    assert load_reference_snapshot(facts.store, plan, facts.uid) is not None
    assert load_reference_snapshot(facts.store, plan, facts.uid, account_role='observer') is None
    assert load_reference_snapshot(facts.store, plan, facts.uid, account_role='admin',
                                   account_tenant='other-tenant') is None


def test_missing_reviewed_pack_preserves_model_fallback_and_blocks_old_replay(facts, monkeypatch, tmp_path):
    assert facts.client.post('/api/assistant/stream', json=body()).status_code == 200
    monkeypatch.setattr('forgeflow.knowledge.reference_facts.default_reference_facts_path',
                        lambda: tmp_path / 'missing.json')
    assert facts.client.post('/api/assistant/stream', json=body(resume=True)).status_code == 409
    assert facts.client.post('/api/assistant/answer', json=body()).json()['mode'] == 'harness'


@pytest.mark.parametrize('revocation', ['disable', 'session', 'role'])
def test_live_account_changes_after_subscription_block_final(facts, monkeypatch, tmp_path, revocation):
    from forgeflow.accounts import AccountStore
    from forgeflow.api.stream_hub import StreamJob
    accounts = AccountStore(tmp_path / 'accounts.sqlite3')
    password = 'Synthetic account test 937!'
    admin, _ = accounts.bootstrap(email='admin@example.invalid', password=password,
                                  tenant_id='facts-tenant')
    code = accounts.invite(admin_principal_id=admin.principal_id,
                           email='reader@example.invalid', role='planner')
    reader, _ = accounts.activate(code=code, password=password)
    bind_odoo_user(facts.store, principal_id=reader.principal_id,
                   tenant_id='facts-tenant', odoo_user_id=3, role='admin')
    change(facts, "INSERT INTO knowledge_role_scopes(tenant_id,role,acl_scope) "
           "VALUES ('facts-tenant','planner','engineering')")
    change(facts, "UPDATE knowledge_documents SET acl_scope='engineering'")
    original = StreamJob.subscribe

    def revoke_then_subscribe(job, last=None):
        if job.metadata.get('kind') == 'reference_facts':
            if revocation == 'disable':
                accounts.disable(admin_principal_id=admin.principal_id, account_id=reader.id)
            elif revocation == 'session':
                accounts.revoke_account(reader.principal_id)
            else:
                accounts.set_role(admin_principal_id=admin.principal_id,
                                  account_id=reader.id, role='observer')
        yield from original(job, last)

    monkeypatch.setattr(StreamJob, 'subscribe', revoke_then_subscribe)
    try:
        with TestClient(create_app(facts.settings, product_executor=FakeProductExecutor(),
                                   accounts=accounts)) as client:
            assert client.post('/api/accounts/login', json={
                'email': reader.email, 'password': password}).status_code == 200
            response = client.post('/api/assistant/stream', json=body())
            assert response.status_code == 200
            assert events(response) == [{'type': 'error', 'code': 'reference_scope_changed'}]
    finally:
        accounts.close()
    assert facts.calls == []

"""Real control database transactions with synthetic ERP/provider seams.

Only a disposable database named forgeflow_workbench_test_* or
forgeflow_admission_test_* is accepted. These tests do not claim live ERP/model
acceptance; the Compose integration suite owns that separate contract.
"""
from __future__ import annotations

import copy
import os
import runpy
import uuid
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlsplit

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row

from forgeflow.local_workbench import bootstrap as module
from forgeflow.superagent.store import ControlStore

ROOT = Path(__file__).resolve().parents[1]
DATABASE_URL = os.environ.get("FORGEFLOW_WORKBENCH_TEST_DATABASE_URL", "")


@pytest.fixture
def store():
    if not DATABASE_URL:
        pytest.skip("disposable workbench test database required")
    name = urlsplit(DATABASE_URL).path.lstrip("/")
    if not name.startswith(("forgeflow_workbench_test_", "forgeflow_admission_test_")):
        pytest.fail("refusing non-test database")
    schema = "workbench_" + uuid.uuid4().hex
    admin = psycopg.connect(DATABASE_URL, autocommit=True)
    admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
    connection = psycopg.connect(DATABASE_URL, row_factory=dict_row,
                                options=f"-c search_path={schema},public")
    result = ControlStore(DATABASE_URL, connection=connection)
    try:
        yield result
    finally:
        result.close()
        admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))
        admin.close()


@pytest.fixture
def config(tmp_path):
    return {"schema_version": 1, "installation_id": str(uuid.uuid4()),
            "operator": {"login": "local", "password": "synthetic-password", "display_name": "Local"},
            "env": {"FORGEFLOW_CONTROL_DATABASE_URL": DATABASE_URL,
                    "FORGEFLOW_CONTROL_TENANT_ID": "forgeflow-local",
                    "FORGEFLOW_SOURCE_PRINCIPAL": "local-source",
                    "FORGEFLOW_APPROVALS_ROOT": str(tmp_path),
                    "FORGEFLOW_ODOO20_LOGIN": "local",
                    "FORGEFLOW_SESSION_SECRET": "synthetic-session-secret-32-characters",
                    "FORGEFLOW_INVITE_CODES": "synthetic-invite-code-1234",
                    "FORGEFLOW_DEPLOYMENT": "local", "FORGEFLOW_SECURE_COOKIE": "false",
                    "QDRANT_URL": "https://qdrant.invalid", "QDRANT_API_KEY": "synthetic",
                    "EMBEDDING_API_URL": "https://embedding.invalid", "EMBEDDING_MODEL_ID": "synthetic",
                    "RERANKER_API_URL": "https://rerank.invalid", "RERANKER_MODEL_ID": "synthetic",
                    "SEMANTIC_API_KEY": "synthetic"}}


@pytest.fixture
def adapter():
    return SimpleNamespace(_authenticate=lambda: 7, user_company_ids=lambda _: (3,),
                           check_contract=lambda: SimpleNamespace(ok=True))


def run(store, config, adapter, install=None):
    return module.bootstrap(config, store=store, adapter=adapter,
                            reference_root=ROOT / "knowledge/reference/supply-recovery-v1",
                            install_pack=install or (lambda *_a, **_k: {"ok": True, "generation": "test"}))


def test_fresh_database_binds_operator_and_defaults_to_no_polling(store, config, adapter):
    result = run(store, config, adapter)
    assert result["ok"] and result["migrations_applied"] > 20
    row = store.connection.execute("SELECT * FROM control_odoo_bindings").fetchone()
    assert row["principal_id"] == module._principal(config)
    assert row["tenant_id"] == "forgeflow-local" and row["odoo_user_id"] == 7
    assert row["role"] == "admin"
    assert store.connection.execute("SELECT enabled FROM control_source_subscriptions").fetchone()["enabled"] is False
    for table in ("control_tickets", "control_effects", "control_assignments", "control_events"):
        assert store.connection.execute(sql.SQL("SELECT count(*) AS n FROM {}").format(
            sql.Identifier(table))).fetchone()["n"] == 0


def test_repeat_preserves_role_subscription_and_does_not_reindex(store, config, adapter):
    run(store, config, adapter)
    with store.transaction() as cursor:
        cursor.execute("UPDATE control_odoo_bindings SET role='observer'")
        cursor.execute("UPDATE control_principals SET role='observer' WHERE id=%s",
                       (module._principal(config),))
        cursor.execute("UPDATE control_source_subscriptions SET enabled=true,interval_seconds=300")
        cursor.execute("UPDATE control_tenants SET policy_version=4")
    second = run(store, config, adapter, install=lambda *_a, **_k: pytest.fail("repeat provider call"))
    assert second["reference_indexed_this_run"] is False and second["migrations_applied"] == 0
    assert store.connection.execute("SELECT role FROM control_odoo_bindings").fetchone()["role"] == "observer"
    row = store.connection.execute("SELECT enabled,interval_seconds FROM control_source_subscriptions").fetchone()
    assert row == {"enabled": True, "interval_seconds": 300}
    assert store.connection.execute("SELECT policy_version FROM control_tenants").fetchone()["policy_version"] == 4


@pytest.mark.parametrize("change", ["installation", "invite", "actor", "company"])
def test_changed_installation_or_identity_never_adopts_existing_authority(store, config, adapter, change):
    run(store, config, adapter)
    other = copy.deepcopy(config)
    if change == "installation":
        other["installation_id"] = str(uuid.uuid4())
    elif change == "invite":
        other["env"]["FORGEFLOW_INVITE_CODES"] = "another-invite-code-1234"
    elif change == "actor":
        adapter._authenticate = lambda: 9
    else:
        adapter.user_company_ids = lambda _: (5,)
    with pytest.raises(module.BootstrapError, match="local_database_installation_mismatch"):
        run(store, other, adapter)
    assert store.connection.execute("SELECT count(*) AS n FROM control_odoo_bindings").fetchone()["n"] == 1


def test_embedding_failure_is_retryable_without_duplicate_identities(store, config, adapter):
    def failed(*_args, **_kwargs):
        raise RuntimeError("synthetic provider unavailable")
    with pytest.raises(RuntimeError, match="synthetic provider unavailable"):
        run(store, config, adapter, failed)
    assert store.connection.execute("SELECT reference_complete FROM forgeflow_local_installation").fetchone()["reference_complete"] is False
    store.connection.commit()
    assert run(store, config, adapter)["ok"]
    assert store.connection.execute("SELECT count(*) AS n FROM control_principals").fetchone()["n"] == 2


def test_revoked_operator_binding_is_not_restored_by_restart(store, config, adapter):
    run(store, config, adapter)
    with store.transaction() as cursor:
        cursor.execute("DELETE FROM control_odoo_bindings")
    with pytest.raises(module.BootstrapError, match="local_operator_binding_changed"):
        run(store, config, adapter)
    assert store.connection.execute("SELECT count(*) AS n FROM control_odoo_bindings").fetchone()["n"] == 0


def test_foreign_control_database_is_not_adopted(store, config, adapter):
    store.migrate()
    with store.transaction() as cursor:
        cursor.execute("INSERT INTO control_tenants(id,display_name) VALUES('another','Existing')")
    with pytest.raises(module.BootstrapError, match="local_database_not_fresh"):
        run(store, config, adapter)
    assert store.connection.execute("SELECT count(*) AS n FROM control_tenants").fetchone()["n"] == 1


def test_multicompany_operator_fails_before_tenant_creation(store, config, adapter):
    adapter.user_company_ids = lambda _: (1, 2)
    with pytest.raises(module.BootstrapError, match="local_operator_company_scope_invalid"):
        run(store, config, adapter)
    assert store.connection.execute("SELECT count(*) AS n FROM control_tenants").fetchone()["n"] == 0


def test_idle_requires_admission_barrier_even_with_empty_queues(store, config, adapter):
    from forgeflow.local_workbench.admission import quiesce

    run(store, config, adapter)
    assert module.idle_status(config, store=store)["ok"] is False
    quiesce(config)
    result = module.idle_status(config, store=store)
    assert result["ok"] and result["admission_locked"]
    assert result["counts"] == {"effects": 0, "assignments": 0, "parse_jobs": 0}


@pytest.mark.parametrize("state", ["pending", "running", "reconcile_required"])
def test_unfinished_or_uncertain_effect_blocks_idle(store, config, adapter, state):
    from forgeflow.local_workbench.admission import quiesce

    run(store, config, adapter)
    quiesce(config)
    thread_id, ticket_id = uuid.uuid4(), uuid.uuid4()
    with store.transaction() as cursor:
        cursor.execute("INSERT INTO control_threads(id,tenant_id,object_ref) VALUES(%s,%s,'synthetic:1')",
                       (thread_id, "forgeflow-local"))
        cursor.execute("INSERT INTO control_tickets(id,tenant_id,thread_id,goal,policy_version) "
                       "VALUES(%s,%s,%s,'Synthetic',1)",
                       (ticket_id, "forgeflow-local", thread_id))
        cursor.execute("INSERT INTO control_effects(id,tenant_id,ticket_id,kind,state) "
                       "VALUES(%s,%s,%s,'synthetic',%s)",
                       (uuid.uuid4(), "forgeflow-local", ticket_id, state))
    result = module.idle_status(config, store=store)
    assert not result["ok"] and result["counts"]["effects"] == 1


def test_active_parse_blocks_idle(store, config, adapter):
    from forgeflow.local_workbench.admission import quiesce

    run(store, config, adapter)
    quiesce(config)
    with store.transaction() as cursor:
        cursor.execute("INSERT INTO knowledge_parse_jobs(id,tenant_id,document_id,source_path,source_sha256) "
                       "VALUES('synthetic-job','forgeflow-local','synthetic-doc','synthetic/path','synthetic')")
    result = module.idle_status(config, store=store)
    assert not result["ok"] and result["counts"]["parse_jobs"] == 1


def test_supervisor_local_start_uses_current_policy_and_never_regrants_source_role(store, config, adapter):
    run(store, config, adapter)
    helper = runpy.run_path(str(ROOT / "scripts/run_supervisor.py"))["_source_context"]
    with store.transaction() as cursor:
        cursor.execute("UPDATE control_tenants SET policy_version=4")
    ctx = helper(store, "forgeflow-local", "local-source", seed_tenant_on_start=False)
    assert ctx.policy_version == 4
    with store.transaction() as cursor:
        cursor.execute("UPDATE control_principals SET role='observer' WHERE id='local-source'")
    with pytest.raises(RuntimeError, match="source_principal_membership_unavailable"):
        helper(store, "forgeflow-local", "local-source", seed_tenant_on_start=False)
    assert store.connection.execute("SELECT role FROM control_principals WHERE id='local-source'").fetchone()["role"] == "observer"

"""Real PostgreSQL acceptance for 024; only an explicitly disposable database.

Run scripts/accept_control_admission_migration.py for mandatory, zero-skip CI.
The normal offline suite may omit this dedicated integration database.
"""
from __future__ import annotations

import os
import shutil
from uuid import uuid4

import psycopg
import pytest
from psycopg import sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from forgeflow.superagent.engine import ControlEngine
from forgeflow.superagent.identity import Role, TenantContext
from forgeflow.superagent.store import DEFAULT_MIGRATIONS, ControlStore

DATABASE_URL = os.environ.get("FORGEFLOW_ADMISSION_TEST_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="dedicated admission test DB not set")
CTX = TenantContext("synthetic-tenant", "planner", frozenset({Role.PLANNER}), 1)


@pytest.fixture(scope="session")
def database():
    with psycopg.connect(DATABASE_URL, autocommit=True) as connection:
        name = connection.execute("SELECT current_database()").fetchone()[0]
        assert name.startswith("forgeflow_admission_test_"), "disposable database required"
        tables = connection.execute(
            "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind IN ('r','p') AND n.nspname NOT IN ('pg_catalog','information_schema') "
            "AND n.nspname NOT LIKE 'pg_toast%' AND n.nspname NOT LIKE 'pg_temp%'"
        ).fetchone()[0]
        assert tables == 0, "empty disposable database required"
    # Never return the DSN as a fixture value: pytest includes fixture arguments
    # in failure tracebacks. The dedicated DB may still use private credentials.


@pytest.fixture
def legacy_store(database, tmp_path):
    schema = "admission_test_" + uuid4().hex
    old = tmp_path / "legacy_migrations"
    old.mkdir()
    for path in DEFAULT_MIGRATIONS.glob("*.sql"):
        if int(path.name.split("_", 1)[0]) < 24:
            shutil.copyfile(path, old / path.name)
    with psycopg.connect(DATABASE_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE SCHEMA {}").format(sql.Identifier(schema)))
        connection = psycopg.connect(
            DATABASE_URL, options=f"-c search_path={schema}", row_factory=dict_row
        )
        store = ControlStore(DATABASE_URL, migrations_dir=old, connection=connection)
        try:
            assert store.migrate() == sorted(int(path.name.split("_", 1)[0]) for path in old.glob("*.sql"))
            store.seed_tenant(CTX.tenant_id, "Synthetic", principals=(("planner", "P", "planner"),))
            store.seed_tenant("other-tenant", "Other")
            connection.commit()
            yield store, old
        finally:
            store.close()
            admin.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(sql.Identifier(schema)))


def admit(store, event, object_id=0):
    return ControlEngine(store).admit_trigger(
        CTX, source_id="odoo20.purchase_order", external_event_id=f"synthetic-{event}",
        payload_hash=f"{event:064x}", object_ref=f"odoo20:purchase.order:{object_id}",
        goal="Synthetic observation", goal_kind="synthetic", initial_effect_kind=None,
    )


def history(store, *, tickets=6, merges=11):
    originals = [admit(store, n, n) for n in range(tickets)]
    attached = [admit(store, tickets + n, n % tickets) for n in range(merges)]
    assert all(item.attached for item in attached)
    assert len({item.ticket_id for item in originals}) == tickets
    assert store.connection.execute(
        "SELECT count(*) AS n FROM control_triggers t WHERE NOT EXISTS "
        "(SELECT 1 FROM control_trigger_admissions a WHERE a.trigger_id=t.id)"
    ).fetchone()["n"] == merges
    store.connection.commit()
    return originals, attached


def snapshot(store):
    """All table contents and sequences, so no effect/event write can hide in a count."""
    connection = store.connection
    tables = connection.execute(
        "SELECT tablename FROM pg_tables WHERE schemaname=current_schema() ORDER BY tablename"
    ).fetchall()
    rows = {}
    for table in tables:
        name = table["tablename"]
        result = connection.execute(sql.SQL(
            "SELECT to_jsonb(t)::text AS body FROM {} t ORDER BY to_jsonb(t)::text COLLATE \"C\""
        ).format(sql.Identifier(name))).fetchall()
        rows[name] = [row["body"] for row in result]
    sequences = {}
    for row in connection.execute(
        "SELECT sequencename FROM pg_sequences WHERE schemaname=current_schema()"
    ).fetchall():
        name = row["sequencename"]
        sequences[name] = connection.execute(sql.SQL(
            "SELECT last_value,is_called FROM {}"
        ).format(sql.Identifier(name))).fetchone()
    connection.commit()
    return rows, sequences


def constraints(store):
    return store.connection.execute(
        "SELECT conname,contype,pg_get_constraintdef(oid) AS definition FROM pg_constraint "
        "WHERE conrelid='control_trigger_admissions'::regclass ORDER BY conname"
    ).fetchall()


def migrate(store):
    store.migrations_dir = DEFAULT_MIGRATIONS
    return store.migrate()


def test_repairs_eleven_old_merges_without_replaying_any_business(legacy_store):
    store, old = legacy_store
    _, attached = history(store)
    with pytest.raises(RuntimeError, match="trigger_admission_missing"):
        admit(store, 6)
    before, sequences = snapshot(store)
    old_constraints = constraints(store)
    assert migrate(store) == [24]
    after, new_sequences = snapshot(store)
    changed = {"control_trigger_admissions", "control_schema_migrations"}
    assert set(after) == set(before) | {"control_trigger_admission_repairs"}
    assert all(after[name] == value for name, value in before.items() if name not in changed)
    assert new_sequences == sequences
    assert len(after["control_trigger_admissions"]) == 17
    assert len(after["control_trigger_admission_repairs"]) == 11
    assert set(before["control_trigger_admissions"]).issubset(after["control_trigger_admissions"])
    assert [row for row in old_constraints if row["contype"] != "u"] == constraints(store)
    for row in store.connection.execute(
        "SELECT a.admitted_at,r.original_admitted_at,r.repaired_at,r.migration_version, "
        "e.created_at FROM control_trigger_admission_repairs r "
        "JOIN control_trigger_admissions a USING(trigger_id) "
        "JOIN control_events e ON e.id=r.event_id"
    ).fetchall():
        assert row["admitted_at"] == row["original_admitted_at"] == row["created_at"]
        assert row["repaired_at"] >= row["original_admitted_at"]
        assert row["migration_version"] == 24
    baseline = snapshot(store)
    for n, expected in enumerate(attached):
        duplicate = admit(store, 6 + n, n % 6)
        assert duplicate.duplicate and duplicate.ticket_id == expected.ticket_id
        assert duplicate.trigger_id == expected.trigger_id
    assert snapshot(store) == baseline
    assert migrate(store) == []
    # Existing v3.9.1 image has only 001..023. It must tolerate recorded 024.
    store.migrations_dir = old
    assert store.migrate() == []
    assert snapshot(store) == baseline
    # Repair history must not prevent the existing tenant cleanup contract.
    with store.transaction() as cursor:
        cursor.execute("DELETE FROM control_tenants WHERE id=%s", (CTX.tenant_id,))
    assert store.connection.execute("SELECT count(*) AS n FROM control_tickets").fetchone()["n"] == 0
    assert store.connection.execute(
        "SELECT count(*) AS n FROM control_trigger_admission_repairs"
    ).fetchone()["n"] == 11


def test_fresh_install_and_future_merges_keep_every_mapping(legacy_store):
    store, _ = legacy_store
    assert migrate(store) == [24]
    first = admit(store, 0)
    merged = [admit(store, n) for n in (1, 2, 3)]
    assert all(item.attached and item.ticket_id == first.ticket_id for item in merged)
    assert store.connection.execute(
        "SELECT count(*) AS n FROM control_trigger_admissions WHERE ticket_id=%s", (first.ticket_id,)
    ).fetchone()["n"] == 4
    before = snapshot(store)
    assert all(admit(store, n).duplicate for n in range(4))
    assert snapshot(store) == before
    assert not before[0]["control_trigger_admission_repairs"]


@pytest.mark.parametrize("damage", [
    "input_tenant", "ticket_tenant", "thread_tenant", "input_hash", "body_hash", "body_source",
    "body_external", "body_object", "input_kind", "input_dedupe", "audit_tenant", "audit_ticket",
    "audit_source", "audit_object", "audit_input", "audit_trigger", "missing_audit",
    "ambiguous_input", "ambiguous_audit", "contradictory_audit", "missing_ticket",
])
def test_unproven_orphan_rolls_back_every_change(legacy_store, damage):
    store, _ = legacy_store
    originals, attached = history(store, tickets=2, merges=2)
    trigger = attached[0].trigger_id
    connection = store.connection
    item = connection.execute("SELECT * FROM control_inputs WHERE trigger_id=%s", (trigger,)).fetchone()
    audit = connection.execute(
        "SELECT * FROM control_events WHERE kind='input_attached' AND payload->>'trigger_id'=%s",
        (trigger,),
    ).fetchone()
    if damage in {"input_tenant", "ticket_tenant", "thread_tenant"}:
        table, identifier = {
            "input_tenant": ("control_inputs", item["id"]),
            "ticket_tenant": ("control_tickets", item["ticket_id"]),
            "thread_tenant": ("control_threads", attached[0].thread_id),
        }[damage]
        connection.execute(sql.SQL("UPDATE {} SET tenant_id='other-tenant' WHERE id=%s").format(
            sql.Identifier(table)), (identifier,))
    elif damage in {"input_hash", "input_kind", "input_dedupe"}:
        column = {"input_hash": "payload_hash", "input_kind": "kind", "input_dedupe": "dedupe_key"}[damage]
        connection.execute(sql.SQL("UPDATE control_inputs SET {}='wrong' WHERE id=%s").format(
            sql.Identifier(column)), (item["id"],))
    elif damage.startswith("body_"):
        key = {"body_hash": "payload_hash", "body_source": "source_id",
               "body_external": "external_event_id", "body_object": "object_ref"}[damage]
        body = {**item["body"], key: "wrong"}
        connection.execute("UPDATE control_inputs SET body=%s WHERE id=%s", (Jsonb(body), item["id"]))
    elif damage in {"audit_tenant", "audit_ticket"}:
        column, value = ("tenant_id", "other-tenant") if damage == "audit_tenant" else (
            "ticket_id", originals[1].ticket_id)
        connection.execute(sql.SQL("UPDATE control_events SET {}=%s,sequence=999 WHERE id=%s").format(
            sql.Identifier(column)), (value, audit["id"]))
    elif damage in {"audit_source", "audit_object", "audit_input", "audit_trigger"}:
        key = {"audit_source": "source_id", "audit_object": "object_ref",
               "audit_input": "input_id", "audit_trigger": "trigger_id"}[damage]
        connection.execute("UPDATE control_events SET payload=%s WHERE id=%s",
                           (Jsonb({**audit["payload"], key: "wrong"}), audit["id"]))
    elif damage == "missing_audit":
        connection.execute("DELETE FROM control_events WHERE id=%s", (audit["id"],))
    elif damage == "ambiguous_input":
        connection.execute(
            "INSERT INTO control_inputs(id,tenant_id,ticket_id,trigger_id,body,payload_hash) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (uuid4(), CTX.tenant_id, item["ticket_id"], trigger, Jsonb(item["body"]), item["payload_hash"]),
        )
    elif damage in {"ambiguous_audit", "contradictory_audit"}:
        payload = dict(audit["payload"])
        if damage == "contradictory_audit":
            payload["trigger_id"] = str(uuid4())
        connection.execute(
            "INSERT INTO control_events(tenant_id,ticket_id,sequence,kind,payload) "
            "VALUES (%s,%s,999,'input_attached',%s)",
            (CTX.tenant_id, item["ticket_id"], Jsonb(payload)),
        )
    elif damage == "missing_ticket":
        connection.execute("DELETE FROM control_tickets WHERE id=%s", (item["ticket_id"],))
    else:
        raise AssertionError(damage)
    connection.commit()
    before = snapshot(store)
    old_constraints = constraints(store)
    with pytest.raises(psycopg.errors.RaiseException, match="unproven_or_ambiguous_orphan"):
        migrate(store)
    connection.rollback()
    assert snapshot(store) == before
    assert constraints(store) == old_constraints


def test_unexpected_additional_unique_index_fails_atomically(legacy_store):
    store, _ = legacy_store
    history(store, tickets=1, merges=1)
    store.connection.execute(
        "CREATE UNIQUE INDEX unexpected_ticket_unique ON control_trigger_admissions(ticket_id)"
    )
    store.connection.commit()
    before = snapshot(store)
    with pytest.raises(psycopg.errors.RaiseException, match="unexpected_ticket_unique_index"):
        migrate(store)
    store.connection.rollback()
    assert snapshot(store) == before
    assert any(row["definition"] == "UNIQUE (ticket_id)" for row in constraints(store))

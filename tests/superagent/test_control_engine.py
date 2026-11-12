"""W1 control-store integration tests. Require a real, explicitly configured PostgreSQL."""
from __future__ import annotations

import os
import uuid
from concurrent.futures import ThreadPoolExecutor

import pytest

from forgeflow.superagent import (
    ControlEngine,
    ControlStore,
    Role,
    StalePolicy,
    TenantContext,
    TriggerConflict,
)
from forgeflow.superagent.engine import ControlNotFound, FencingError

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL, reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def _store() -> ControlStore:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    return store


def _context(tenant: str, principal: str = "planner-a", policy: int = 1) -> TenantContext:
    return TenantContext(tenant, principal, frozenset({Role.PLANNER}), policy)


@pytest.fixture
def tenant():
    tenant_id = f"w1-{uuid.uuid4().hex}"
    store = _store()
    store.seed_tenant(tenant_id, "W1 test", 1, (("planner-a", "Planner A", "planner"),))
    store.close()
    yield tenant_id
    store = _store()
    with store.connection.transaction():
        store.connection.execute("DELETE FROM control_tenants WHERE id=%s", (tenant_id,))
    store.close()


def _admit(store: ControlStore, tenant_id: str, event: str = "event-1", digest: str = "a" * 64):
    return ControlEngine(store).admit_trigger(
        _context(tenant_id), source_id="workbench", external_event_id=event,
        payload_hash=digest, object_ref="PO-1", goal="investigate",
    )


def test_duplicate_event_creates_one_ticket_and_reuses_it(tenant: str) -> None:
    store = _store()
    try:
        first = _admit(store, tenant)
        second = _admit(store, tenant)
        assert first.ticket_id == second.ticket_id
        assert second.duplicate is True
        assert store.connection.execute(
            "SELECT count(*) AS count FROM control_tickets WHERE tenant_id=%s",
            (tenant,)).fetchone()["count"] == 1
        effects = store.connection.execute(
            "SELECT count(*) AS count FROM control_effects WHERE tenant_id=%s",
            (tenant,)).fetchone()["count"]
        assert effects == 1
    finally:
        store.close()


def test_concurrent_duplicate_event_has_one_admission(tenant: str) -> None:
    def admit():
        store = _store()
        try:
            return _admit(store, tenant, event="concurrent-event")
        finally:
            store.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: admit(), range(2)))
    assert results[0].ticket_id == results[1].ticket_id
    assert sum(not result.duplicate for result in results) == 1


def test_same_external_id_with_different_body_is_rejected(tenant: str) -> None:
    store = _store()
    try:
        _admit(store, tenant, digest="a" * 64)
        with pytest.raises(TriggerConflict):
            _admit(store, tenant, digest="b" * 64)
    finally:
        store.close()


def test_stale_policy_is_rejected(tenant: str) -> None:
    store = _store()
    try:
        with pytest.raises(StalePolicy):
            ControlEngine(store).admit_trigger(
                _context(tenant, policy=2), source_id="workbench", external_event_id="stale",
                payload_hash="c" * 64, object_ref="PO-1", goal="investigate")
    finally:
        store.close()


def test_cross_tenant_ticket_access_is_denied(tenant: str) -> None:
    store = _store()
    try:
        other = f"w1-other-{uuid.uuid4().hex}"
        store.seed_tenant(other, "Other", 1, (("planner-b", "Planner B", "planner"),))
        ticket = _admit(store, tenant)
        with pytest.raises(ControlNotFound):
            ControlEngine(store).get_ticket(_context(other, "planner-b"), ticket.ticket_id)
    finally:
        store.close()


def test_pending_effect_survives_restart_and_fencing_is_enforced(tenant: str) -> None:
    store = _store()
    ticket = _admit(store, tenant, event="restart-event")
    store.close()

    restarted = _store()
    try:
        engine = ControlEngine(restarted)
        effect = engine.claim_effect("worker-a", lease_seconds=60, tenant_id=tenant)
        assert effect is not None and effect["ticket_id"] == ticket.ticket_id
        with pytest.raises(FencingError):
            engine.complete_effect(effect_id=effect["id"], tenant_id=tenant,
                                   fencing_token="00000000-0000-0000-0000-000000000000")
        engine.complete_effect(effect_id=effect["id"], tenant_id=tenant,
                               fencing_token=effect["fencing_token"])
        assert engine.pending_effect_count(tenant) == 0
        assert [row["kind"] for row in engine.ticket_events(_context(tenant), ticket.ticket_id)] == [
            "ticket_admitted", "effect_completed"]
    finally:
        restarted.close()

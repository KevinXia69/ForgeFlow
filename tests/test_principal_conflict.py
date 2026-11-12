"""A principal id is global: it may not be silently rebound to another tenant."""
from __future__ import annotations

import os
import uuid

import pytest

from forgeflow.superagent import ControlStore, bind_odoo_user

DATABASE_URL = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
pytestmark = pytest.mark.skipif(not DATABASE_URL,
                                reason="FORGEFLOW_CONTROL_DATABASE_URL not set")


def test_the_same_principal_id_is_tenant_scoped() -> None:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    first = f"pconf-a-{uuid.uuid4().hex}"
    second = f"pconf-b-{uuid.uuid4().hex}"
    principal = f"shared-{uuid.uuid4().hex}"
    try:
        store.seed_tenant(first, "First", 1, ((principal, "Shared", "planner"),))
        store.seed_tenant(second, "Second", 1,
                          ((principal, "Shared", "planner"),))
        rows = store.connection.execute(
            "SELECT tenant_id FROM control_principals WHERE id=%s ORDER BY tenant_id",
            (principal,)).fetchall()
        store.connection.commit()
        assert [row["tenant_id"] for row in rows] == sorted([first, second])
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_tenants WHERE id=ANY(%s)", ([first, second],))
            store.connection.execute(
                "DELETE FROM control_odoo_bindings WHERE principal_id=%s", (principal,))
            store.connection.execute(
                "DELETE FROM control_principals WHERE id=%s", (principal,))
        store.close()


def test_a_binding_cannot_move_a_principal_to_another_tenant() -> None:
    store = ControlStore(DATABASE_URL)
    store.migrate()
    first = f"pconf-c-{uuid.uuid4().hex}"
    second = f"pconf-d-{uuid.uuid4().hex}"
    principal = f"bound-{uuid.uuid4().hex}"
    try:
        store.seed_tenant(first, "First", 1, ((principal, "Bound", "planner"),))
        store.seed_tenant(second, "Second", 1)
        bind_odoo_user(store, principal_id=principal, tenant_id=first,
                       odoo_user_id=2, role="approver")
        with pytest.raises(ValueError, match="principal_tenant_conflict"):
            bind_odoo_user(store, principal_id=principal, tenant_id=second,
                           odoo_user_id=2, role="approver")
        row = store.connection.execute(
            "SELECT tenant_id FROM control_odoo_bindings WHERE principal_id=%s",
            (principal,)).fetchone()
        store.connection.commit()
        assert row is not None and row["tenant_id"] == first
    finally:
        with store.connection.transaction():
            store.connection.execute(
                "DELETE FROM control_tenants WHERE id=ANY(%s)", ([first, second],))
            store.connection.execute(
                "DELETE FROM control_odoo_bindings WHERE principal_id=%s", (principal,))
            store.connection.execute(
                "DELETE FROM control_principals WHERE id=%s", (principal,))
        store.close()

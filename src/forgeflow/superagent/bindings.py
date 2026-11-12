"""Server-side binding between an authenticated principal and an Odoo user."""
from __future__ import annotations

from ..superagent.store import ControlStore


def bind_odoo_user(store: ControlStore, *, principal_id: str, tenant_id: str,
                   odoo_user_id: int, role: str = "approver") -> None:
    if not principal_id or not tenant_id or int(odoo_user_id) <= 0:
        raise ValueError("invalid_binding")
    if role not in {"planner", "approver", "admin", "observer"}:
        raise ValueError("invalid_role")
    with store.transaction() as cursor:
        existing = cursor.execute(
            "SELECT tenant_id FROM control_odoo_bindings WHERE principal_id=%s",
            (principal_id,)).fetchone()
        if existing is not None and str(existing["tenant_id"]) != str(tenant_id):
            # The binding table is keyed by principal alone; moving a bound login
            # to another tenant would make principal_context ambiguous.
            raise ValueError("principal_tenant_conflict")
        cursor.execute(
            "INSERT INTO control_odoo_bindings(principal_id, tenant_id, odoo_user_id, role) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (principal_id) DO UPDATE SET "
            "tenant_id=EXCLUDED.tenant_id, odoo_user_id=EXCLUDED.odoo_user_id, role=EXCLUDED.role",
            (principal_id, tenant_id, int(odoo_user_id), role))
        cursor.execute(
            "INSERT INTO control_principals(id, tenant_id, display_name, role) "
            "VALUES (%s, %s, %s, %s) ON CONFLICT (tenant_id, id) DO UPDATE SET "
            "display_name=EXCLUDED.display_name, role=EXCLUDED.role",
            (principal_id, tenant_id, f"Odoo user {int(odoo_user_id)}", role))


def tenant_for_principal(store: ControlStore, *, principal_id: str) -> str | None:
    row = store.connection.execute(
        "SELECT tenant_id FROM control_odoo_bindings WHERE principal_id=%s",
        (principal_id,)).fetchone()
    value = str(row["tenant_id"]) if row else None
    store.connection.commit()
    return value


def role_for_principal(store: ControlStore, *, principal_id: str) -> str | None:
    row = store.connection.execute(
        "SELECT role FROM control_odoo_bindings WHERE principal_id=%s", (principal_id,)).fetchone()
    value = str(row["role"]) if row else None
    store.connection.commit()
    return value


def odoo_user_id_for(store: ControlStore, *, principal_id: str,
                     tenant_id: str) -> int | None:
    row = store.connection.execute(
        "SELECT odoo_user_id FROM control_odoo_bindings "
        "WHERE principal_id=%s AND tenant_id=%s", (principal_id, tenant_id)).fetchone()
    value = int(row["odoo_user_id"]) if row else None
    store.connection.commit()
    return value


def principal_context(store: ControlStore, *, principal_id: str):
    """Resolve an authenticated principal into a server-derived tenant context.

    The role and tenant come from the control store binding, never from the
    request.  A principal with no binding gets no context at all.
    """
    from .identity import ObjectScope, Role, TenantContext

    row = store.connection.execute(
        "SELECT b.tenant_id, b.role, t.policy_version "
        "FROM control_odoo_bindings b JOIN control_tenants t ON t.id=b.tenant_id "
        "WHERE b.principal_id=%s", (principal_id,)).fetchone()
    store.connection.commit()
    if row is None:
        return None
    try:
        role = Role(str(row["role"]))
    except ValueError:
        return None
    return TenantContext(str(row["tenant_id"]), principal_id, frozenset({role}),
                         int(row["policy_version"]), ObjectScope.all_objects())

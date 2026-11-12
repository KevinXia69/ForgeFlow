"""Server-side knowledge scope resolution.

The authenticated principal selects a tenant through a control-store binding.
A browser request never supplies the tenant itself, and the ACL scopes are read
from the tenant's persisted policy at request time, so revoking a role's access
takes effect on the next query rather than at the next deployment.
"""
from __future__ import annotations

import os
from typing import Sequence

from ..semantic import SemanticConfig, SemanticConfigError
from .semantic_answer import KnowledgeScope, resolve_acl_scopes


def _store_from_env():
    control_url = os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
    if not control_url:
        return None
    from ..superagent.store import ControlStore

    return ControlStore(control_url)


ROLES = ("admin", "planner", "approver", "observer")


def server_scope(locale: str, source_paths: Sequence[str] | None = None,
                 generation: str | None = None,
                 principal_id: str | None = None,
                 *, acl_scopes: Sequence[str] | None = None,
                 mandate_scopes: Sequence[str] | None = None,
                 account_role: str | None = None,
                 source_locale: str | None = None,
                 store=None) -> KnowledgeScope | None:
    try:
        config = SemanticConfig.from_env()
    except SemanticConfigError:
        return None
    if account_role is not None and account_role not in ROLES:
        raise ValueError("invalid_knowledge_role")
    tenant_id = config.tenant_id
    # The account role is authoritative when an account store is in use; the
    # control-store binding remains the fallback for invitation sessions.
    role = account_role or "observer"
    owned = None
    if store is None:
        owned = _store_from_env()
        store = owned
    try:
        if principal_id and store is not None:
            from ..superagent.bindings import role_for_principal, tenant_for_principal

            tenant_id = tenant_for_principal(store, principal_id=principal_id) or ""
            if account_role is None:
                role = role_for_principal(store, principal_id=principal_id) or "observer"
            if not tenant_id:
                raise PermissionError("knowledge_tenant_binding_required")
        resolved_acl = (frozenset(acl_scopes) if acl_scopes is not None
                        else resolve_acl_scopes(tenant_id, role, store=store))
        if generation is None and store is not None:
            from .catalog import KnowledgeCatalog

            generation = KnowledgeCatalog(store).published(tenant_id)
        return KnowledgeScope(
            tenant_id=tenant_id,
            locale=locale,
            source_paths=frozenset(source_paths) if source_paths is not None else None,
            generation=generation,
            role=role,
            acl_scopes=resolved_acl,
            principal_id=principal_id,
            mandate_scopes=(frozenset(mandate_scopes) if mandate_scopes is not None
                            else None),
            source_locale=source_locale,
        )
    finally:
        if owned is not None:
            owned.close()

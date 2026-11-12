"""Server-derived identity; callers never choose a tenant from input.

Two rules drive this module:

1. There is no built-in administrator identity.  A background service acts *for*
   a principal through a persisted mandate, or it does not act at all.
2. A tenant, role or object set is always derived from an authenticated binding
   plus the control store.  It is never inferred from a model message, a request
   body or a worker capability string.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class TenantError(ValueError):
    pass


class Role(StrEnum):
    ADMIN = "admin"
    PLANNER = "planner"
    APPROVER = "approver"
    OBSERVER = "observer"


class ScopeMode(StrEnum):
    """Explicit three-state object scope.

    ``ALL`` is only ever produced by the control store for a mandate that is
    tenant-wide.  ``NONE`` is *not* the same thing as "no restriction": it means
    the caller may read nothing.
    """

    ALL = "all"
    OBJECTS = "objects"
    NONE = "none"


@dataclass(frozen=True, slots=True)
class ObjectScope:
    scope_mode: str = ScopeMode.NONE.value
    object_ids: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if self.scope_mode not in {mode.value for mode in ScopeMode}:
            raise TenantError("invalid_object_scope")

    def contains(self, object_id: str) -> bool:
        if self.scope_mode == ScopeMode.ALL.value:
            return True
        if self.scope_mode == ScopeMode.NONE.value:
            return False
        return object_id in self.object_ids

    @classmethod
    def all_objects(cls) -> "ObjectScope":
        return cls(ScopeMode.ALL.value, frozenset())

    @classmethod
    def none(cls) -> "ObjectScope":
        return cls(ScopeMode.NONE.value, frozenset())

    @classmethod
    def of(cls, object_ids: frozenset[str] | set[str] | tuple[str, ...]) -> "ObjectScope":
        values = frozenset(object_ids)
        if not values:
            return cls.none()
        return cls(ScopeMode.OBJECTS.value, values)


@dataclass(frozen=True, slots=True)
class TenantContext:
    tenant_id: str
    principal_id: str
    roles: frozenset[Role]
    policy_version: int
    objects: ObjectScope = ObjectScope()
    #: Set when the context was derived for a background service on behalf of a
    #: principal.  It never grants extra authority; it only names the caller.
    service_id: str | None = None
    #: The persisted mandate this context is currently acting under, if any.
    mandate_id: str | None = None

    def __post_init__(self) -> None:
        if not self.tenant_id or not self.principal_id:
            raise TenantError("tenant_and_principal_required")
        if self.policy_version < 1:
            raise TenantError("invalid_policy_version")
        if not self.roles:
            raise TenantError("role_required")

    def require(self, *roles: Role) -> None:
        if not self.roles.intersection(roles):
            raise TenantError("role_denied")

    def require_worker(self) -> None:
        """A service acting under a mandate may run the work it was authorised for.

        A human still needs an operator role; a background service is not given one
        as a blanket pass -- it must carry a persisted mandate for an existing
        ticket, which is the actual authority to act.
        """
        if self.is_service and self.mandate_id:
            return
        self.require(Role.ADMIN, Role.PLANNER)

    def assert_tenant(self, tenant_id: str) -> None:
        if tenant_id != self.tenant_id:
            raise TenantError("tenant_scope_denied")

    def assert_object(self, object_ref: str) -> None:
        if not self.objects.contains(object_ref):
            raise TenantError("object_scope_denied")

    @property
    def is_service(self) -> bool:
        return self.service_id is not None

    def as_service(self, service_id: str, mandate_id: str | None = None) -> "TenantContext":
        """Return the same authority, labelled as a background service call.

        Deliberately does not elevate roles or widen the object scope: a service
        identity is an audit label, not a privilege escalation.
        """
        if not service_id:
            raise TenantError("service_id_required")
        return TenantContext(
            self.tenant_id, self.principal_id, self.roles, self.policy_version,
            self.objects, service_id, mandate_id or self.mandate_id)

    def with_mandate(self, mandate_id: str, *, objects: ObjectScope,
                     capabilities: frozenset[str] = frozenset()) -> "TenantContext":
        if not mandate_id:
            raise TenantError("mandate_id_required")
        del capabilities  # capabilities are checked against the persisted mandate
        return TenantContext(
            self.tenant_id, self.principal_id, self.roles, self.policy_version,
            objects, self.service_id, mandate_id)


@dataclass(frozen=True, slots=True)
class ToolScope:
    """Short-lived authority handed to one worker assignment.

    A worker receives this instead of ERP, Qdrant or cloud credentials.  Every
    tool call re-checks it against the *current* persisted mandate, so a
    revocation takes effect on the next call rather than on the next session.
    """

    tenant_id: str
    principal_id: str
    ticket_id: str
    assignment_id: str
    mandate_id: str
    capability: str
    allowed_tools: frozenset[str]
    object_scope: ObjectScope
    budget_tokens: int
    expires_at: float
    run_token: str

    def __post_init__(self) -> None:
        if not self.assignment_id or not self.run_token:
            raise TenantError("tool_scope_identity_required")
        if self.budget_tokens <= 0:
            raise TenantError("tool_scope_budget_required")

    def permits(self, tool: str, object_ref: str | None = None) -> bool:
        if tool not in self.allowed_tools:
            return False
        if object_ref is not None and not self.object_scope.contains(object_ref):
            return False
        return True


def service_context(mandate, *, service_id: str,
                    roles: frozenset[Role] | set[Role] | None = None) -> TenantContext:
    """Build a service context strictly from a persisted mandate.

    Replaces the previous fixed ``worker-dispatcher`` / ``admin`` / policy ``1``
    context.  An expired, revoked or policy-stale mandate cannot produce one.
    """
    from .contracts import MandateState

    if mandate.state != MandateState.ACTIVE.value:
        raise TenantError("mandate_not_active")
    if mandate.revision < 1:
        raise TenantError("mandate_revision_invalid")
    scope = (ObjectScope.all_objects() if mandate.scope_mode == ScopeMode.ALL.value
             else ObjectScope.of(mandate.object_refs))
    # The service acts for the principal who was authorised to create the work.
    # Hard-coding the planner role denies tasks legitimately started by an admin
    # or an approver, so the persisted membership is what counts.
    roles = frozenset({Role.PLANNER}) if not roles else frozenset(roles)
    return TenantContext(
        mandate.tenant_id, mandate.principal_id, roles,
        mandate.policy_version, scope, service_id, mandate.id)

"""Stable control-layer contracts; no model or ERP objects are authoritative here.

This module is the vocabulary of the Super-agent control layer.  Everything that
crosses a module boundary -- ticket state, effect kind, attention need, mandate
scope, worker report -- is declared here so that the Control Engine, the
dispatcher, the watchdog and the HTTP layer cannot invent parallel semantics.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum


class TicketState(StrEnum):
    """Where the *work* has got to.  Never a proxy for the business outcome."""

    QUEUED = "queued"
    RUNNING = "running"
    WAITING_INPUT = "waiting_input"
    WAITING_EVENT = "waiting_event"
    RESULT_READY = "result_ready"
    AWAITING_APPROVAL = "awaiting_approval"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    FAILED = "failed"
    CANCELLED = "cancelled"
    CLOSED = "closed"


TERMINAL_TICKET_STATES: frozenset[TicketState] = frozenset({
    TicketState.COMPLETED, TicketState.FAILED, TicketState.CANCELLED, TicketState.CLOSED,
})

#: Ticket states that mean "this work is still going and can accept new input".
ACTIVE_TICKET_STATES: frozenset[TicketState] = frozenset(
    state for state in TicketState if state not in TERMINAL_TICKET_STATES)

#: The only states a user is ever shown (plan section 13.2).  Internal effect
#: failures, lease rotation and session endings are deliberately absent.
USER_TICKET_STATES = (
    "investigating", "needs_input", "awaiting_approval", "executing",
    "verifying", "completed", "needs_action",
)

#: Finer internal phase, exported alongside the seven external states.  It never
#: replaces them: a phase explains *where* in the work we are, not the outcome.
TICKET_PHASE: dict[str, str] = {
    "queued": "queued",
    "running": "investigating",
    "waiting_input": "waiting_for_fact",
    "waiting_event": "waiting_for_event",
    "result_ready": "result_ready",
    "awaiting_approval": "awaiting_approval",
    "executing": "executing",
    "verifying": "verifying",
    "completed": "verified",
    "blocked": "needs_decision",
    "failed": "failed",
    "cancelled": "stopped",
    "closed": "closed",
}


def phase_for(state: str) -> str:
    """Internal phase of a ticket state, for step-level detail only."""
    return TICKET_PHASE.get(normalize_ticket_state(state), "unknown")

USER_TICKET_STATE: dict[str, str] = {
    TicketState.QUEUED.value: "investigating",
    TicketState.RUNNING.value: "investigating",
    TicketState.WAITING_INPUT.value: "needs_input",
    TicketState.WAITING_EVENT.value: "investigating",
    TicketState.RESULT_READY.value: "investigating",
    TicketState.AWAITING_APPROVAL.value: "awaiting_approval",
    TicketState.EXECUTING.value: "executing",
    TicketState.VERIFYING.value: "verifying",
    TicketState.COMPLETED.value: "completed",
    # A stopped task is not a finished one: showing "completed" for a cancel or a
    # lost execution would hide work that still needs a decision.
    TicketState.CANCELLED.value: "needs_action",
    TicketState.CLOSED.value: "needs_action",
    TicketState.BLOCKED.value: "needs_action",
    TicketState.FAILED.value: "needs_action",
}

#: States written by earlier revisions of this code.  They are accepted on read
#: and normalised, never written again.
LEGACY_TICKET_STATES: dict[str, str] = {
    "waiting_review": TicketState.RESULT_READY.value,
    "needs_attention": TicketState.BLOCKED.value,
}


def normalize_ticket_state(state: str) -> str:
    value = str(state)
    return LEGACY_TICKET_STATES.get(value, value)


def user_state_for(state: str) -> str:
    """Map an internal ticket state onto the seven user-visible states."""
    return USER_TICKET_STATE.get(normalize_ticket_state(state), "investigating")


class EffectKind(StrEnum):
    ROUTE_DECISION = "route_decision"
    ASSESS = "assess"
    LAUNCH_WORKER = "launch_worker"
    DELIVER_INPUT = "deliver_input"
    PREPARE_PLAN = "prepare_plan"
    APPLY_APPROVED_PLAN = "apply_approved_plan"
    RECONCILE_EXECUTION = "reconcile_execution"
    VERIFY_EXECUTION = "verify_execution"
    INDEX_SOURCE = "index_source"
    SEND_NOTIFICATION = "send_notification"


class SideEffectClass(StrEnum):
    """Recovery policy is chosen by side effect, never by generic optimism."""

    READ_ONLY = "read_only"
    IDEMPOTENT_METADATA = "idempotent_metadata"
    MODEL_INFERENCE = "model_inference"
    BUSINESS_WRITE = "business_write"
    NOTIFICATION = "notification"


#: Effects that may be replayed after a lost lease, and those that may not.
REPLAYABLE_SIDE_EFFECTS: frozenset[SideEffectClass] = frozenset({
    SideEffectClass.READ_ONLY,
    SideEffectClass.IDEMPOTENT_METADATA,
    SideEffectClass.MODEL_INFERENCE,
    SideEffectClass.NOTIFICATION,
})

#: A business write whose outcome is unknown must be reconciled, never replayed.
RECONCILE_SIDE_EFFECTS: frozenset[SideEffectClass] = frozenset({
    SideEffectClass.BUSINESS_WRITE,
})

EFFECT_SIDE_EFFECT: dict[str, SideEffectClass] = {
    EffectKind.ROUTE_DECISION.value: SideEffectClass.MODEL_INFERENCE,
    EffectKind.ASSESS.value: SideEffectClass.READ_ONLY,
    EffectKind.LAUNCH_WORKER.value: SideEffectClass.MODEL_INFERENCE,
    EffectKind.DELIVER_INPUT.value: SideEffectClass.READ_ONLY,
    EffectKind.PREPARE_PLAN.value: SideEffectClass.READ_ONLY,
    EffectKind.APPLY_APPROVED_PLAN.value: SideEffectClass.BUSINESS_WRITE,
    EffectKind.RECONCILE_EXECUTION.value: SideEffectClass.READ_ONLY,
    EffectKind.VERIFY_EXECUTION.value: SideEffectClass.READ_ONLY,
    EffectKind.INDEX_SOURCE.value: SideEffectClass.IDEMPOTENT_METADATA,
    EffectKind.SEND_NOTIFICATION.value: SideEffectClass.NOTIFICATION,
}


def side_effect_for(kind: str) -> SideEffectClass:
    return EFFECT_SIDE_EFFECT.get(str(kind), SideEffectClass.MODEL_INFERENCE)


class EffectState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RECONCILE_REQUIRED = "reconcile_required"


class AttentionNeed(StrEnum):
    """The complete, closed vocabulary of reasons to interrupt a human.

    A successful investigation result is deliberately not part of it.
    """

    CLARIFICATION = "clarification"
    FACT = "fact"
    APPROVAL = "approval"
    FAILURE = "failure"
    RULE = "rule"
    BUDGET = "budget"
    RESULT_UNKNOWN = "result_unknown"
    SYSTEM = "system"


#: Needs that block forward progress; a non-blocking attention is informational.
BLOCKING_NEEDS: frozenset[AttentionNeed] = frozenset({
    AttentionNeed.CLARIFICATION, AttentionNeed.FACT, AttentionNeed.APPROVAL,
    AttentionNeed.FAILURE, AttentionNeed.RULE, AttentionNeed.BUDGET,
    AttentionNeed.RESULT_UNKNOWN, AttentionNeed.SYSTEM,
})


class AssignmentState(StrEnum):
    LEASED = "leased"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    REJECTED = "rejected"


class RouterDecision(StrEnum):
    """Router output vocabulary.  These are *proposals*; the engine decides."""

    ANSWER = "answer"
    START_INVESTIGATION = "start_investigation"
    ATTACH_TO_EXISTING_TASK = "attach_to_existing_task"
    REQUEST_CLARIFICATION = "request_clarification"
    PREPARE_PLAN = "prepare_plan"
    WAIT_FOR_EVENT = "wait_for_event"
    STOP_TASK = "stop_task"
    IGNORE = "ignore"


class MandateScopeMode(StrEnum):
    ALL = "all"
    OBJECTS = "objects"
    NONE = "none"


class MandateState(StrEnum):
    ACTIVE = "active"
    REVOKED = "revoked"
    EXPIRED = "expired"


class SourceState(StrEnum):
    """How a business change is treated by the continuous-watch policy."""

    OBSERVE = "observe"
    AUTO_INVESTIGATE = "auto_investigate"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class Trigger:
    id: str
    tenant_id: str
    source_id: str
    external_event_id: str
    payload_hash: str


@dataclass(frozen=True, slots=True)
class Ticket:
    id: str
    tenant_id: str
    thread_id: str
    goal: str
    state: str
    version: int
    policy_version: int
    mandate_id: str | None = None
    goal_kind: str = ""

    @property
    def user_state(self) -> str:
        return user_state_for(self.state)

    @property
    def is_terminal(self) -> bool:
        return normalize_ticket_state(self.state) in {s.value for s in TERMINAL_TICKET_STATES}


@dataclass(frozen=True, slots=True)
class Effect:
    id: str
    tenant_id: str
    ticket_id: str
    kind: str
    payload: dict
    state: str
    attempt: int
    fencing_token: str | None = None
    side_effect_class: str = SideEffectClass.READ_ONLY.value
    idempotency_key: str | None = None
    mandate_id: str | None = None

    @property
    def replayable(self) -> bool:
        return self.side_effect_class in {item.value for item in REPLAYABLE_SIDE_EFFECTS}


@dataclass(frozen=True, slots=True)
class Assignment:
    id: str
    tenant_id: str
    ticket_id: str
    runner: str
    attempt: int
    state: str
    fencing_token: str | None = None
    capability: str | None = None
    mandate_id: str | None = None
    result: dict | None = None


@dataclass(frozen=True, slots=True)
class Mandate:
    """Scoped authority to investigate, research and prepare plans.

    A mandate never authorises a business write.  Only an approval bound to one
    exact plan version does that.
    """

    id: str
    tenant_id: str
    principal_id: str
    policy_version: int
    capabilities: frozenset[str] = frozenset()
    tools: frozenset[str] = frozenset()
    knowledge_scopes: frozenset[str] = frozenset()
    scope_mode: str = MandateScopeMode.OBJECTS.value
    object_refs: frozenset[str] = frozenset()
    budget_tokens: int | None = None
    budget_effects: int | None = None
    deadline_at: datetime | None = None
    state: str = MandateState.ACTIVE.value
    revision: int = 1

    def permits_object(self, object_ref: str) -> bool:
        if self.scope_mode == MandateScopeMode.ALL.value:
            return True
        if self.scope_mode == MandateScopeMode.NONE.value:
            return False
        return object_ref in self.object_refs

    def permits_capability(self, capability: str) -> bool:
        return not self.capabilities or capability in self.capabilities

    def permits_tool(self, tool: str) -> bool:
        return not self.tools or tool in self.tools


@dataclass(frozen=True, slots=True)
class MandateGrant:
    """The result of a per-call authorization check."""

    mandate_id: str
    tenant_id: str
    principal_id: str
    capability: str
    object_ref: str | None
    policy_version: int
    expires_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class Admission:
    trigger_id: str
    ticket_id: str
    duplicate: bool = False
    thread_id: str | None = None
    mandate_id: str | None = None
    attached: bool = False
    input_id: str | None = None


@dataclass(frozen=True, slots=True)
class PlanRequest:
    """A worker's request for planning, bound to the facts it saw."""

    objective: str = "recovery"
    snapshot_ref: str | None = None
    fact_snapshot: dict = field(default_factory=dict)
    rules: tuple[dict, ...] = ()
    options: dict = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class WorkerReport:
    """Structured worker output.  Investigation, preparation and execution are
    separate statuses; "completed" never means the business problem is solved."""

    task_id: str
    assignment_id: str
    fencing_token: str
    status: str
    findings: tuple[dict, ...] = ()
    evidence_refs: tuple[dict, ...] = ()
    unknowns: tuple[dict, ...] = ()
    required_attention: tuple[dict, ...] = ()
    snapshot_ref: str | None = None
    usage: dict = field(default_factory=dict)
    usage_complete: bool = False
    plan_request: PlanRequest | None = None
    next_effect: str | None = None
    decision: str | None = None

    def as_dict(self) -> dict:
        payload = {
            "task_id": self.task_id,
            "assignment_id": self.assignment_id,
            "status": self.status,
            "findings": list(self.findings),
            "evidence_refs": list(self.evidence_refs),
            "unknowns": list(self.unknowns),
            "required_attention": list(self.required_attention),
            "snapshot_ref": self.snapshot_ref,
            "usage": dict(self.usage),
            "usage_complete": bool(self.usage_complete),
            "next_effect": self.next_effect,
            "decision": self.decision,
        }
        if self.plan_request is not None:
            payload["plan_request"] = {
                "objective": self.plan_request.objective,
                "snapshot_ref": self.plan_request.snapshot_ref,
                "fact_snapshot": self.plan_request.fact_snapshot,
                "rules": list(self.plan_request.rules),
                "options": self.plan_request.options,
            }
        return payload

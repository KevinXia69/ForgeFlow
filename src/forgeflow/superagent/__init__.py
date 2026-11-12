"""Transactional control layer for the ForgeFlow Business Super-agent."""
from .bindings import (
    bind_odoo_user,
    odoo_user_id_for,
    principal_context,
    role_for_principal,
    tenant_for_principal,
)
from .contracts import (
    ACTIVE_TICKET_STATES,
    TERMINAL_TICKET_STATES,
    USER_TICKET_STATE,
    USER_TICKET_STATES,
    AssignmentState,
    AttentionNeed,
    EffectKind,
    EffectState,
    Mandate,
    MandateScopeMode,
    MandateState,
    RouterDecision,
    SideEffectClass,
    TicketState,
    normalize_ticket_state,
    phase_for,
    side_effect_for,
    user_state_for,
)
from .dispatcher import Dispatcher, DispatcherLoop
from .engine import (
    BudgetExhausted,
    ControlEngine,
    ControlNotFound,
    FencingError,
    LateResultRejected,
    MandateDenied,
    StalePolicy,
    StateTransitionRejected,
    TriggerConflict,
)
from .execution import approve_plan, reconcile_execution, request_execution, revoke_approval
from .heartbeat import beat, health
from .identity import ObjectScope, Role, ScopeMode, TenantContext, TenantError, ToolScope
from .lease import LeaseKeeper
from .model_runners import (
    FallbackModelRunner,
    HttpModelRunner,
    ModelRunnerError,
    PiJsonRunner,
    RunnerUsage,
    parse_pi_usage,
)
from .outbox import OutboxConsumer, OutboxError
from .pi_docker import PiDockerRunner
from .plan_effects import PlanEffects
from .source_runner import SourceRunner
from .source_service import SourceService
from .sources import (
    ConditionSource,
    ConditionSourceLoop,
    KnowledgeGenerationSource,
    OdooPurchaseSource,
    ScanReport,
    SourceEvent,
    SourceLoop,
    SourceScanner,
    SourceStateStore,
    admit_events,
    refresh_observed_state,
)
from .store import ControlStore
from .subscriptions import (
    SubscriptionError,
    ensure_subscription,
    list_subscriptions,
    upsert_subscription,
)
from .supervisor import Supervisor
from .tasks import (
    PlanTask,
    answer_attention,
    answer_attention_by_id,
    cancel_ticket,
    link_plan,
    list_failure_attentions,
    list_open_attentions,
    list_plan_tasks,
    list_tickets,
    open_attention,
    plan_task,
    retry_failed_ticket,
)
from .tools import (
    TOOL_SPECS,
    TOOL_SPECS_BY_NAME,
    CompositeToolBox,
    InvestigationToolBox,
    KnowledgeToolBox,
    OdooReadToolBox,
    ToolBox,
    WebSearchToolBox,
    investigation_tool_specs,
    knowledge_tool_specs,
    odoo_tool_specs,
    specs_for,
    tool_manifest,
    web_tool_specs,
)
from .watchdog import EffectWatchdog, ProcessWatchdog, RecoveryPolicy, WatchdogLoop
from .workers import (
    CallableWorker,
    DshWorker,
    HeuristicRouterPolicy,
    ModelReply,
    ModelRouterPolicy,
    ModelRunner,
    PiWorker,
    Router,
    RouterDirective,
    RouterRequest,
    StructuredModelWorker,
    ToolLoopWorker,
    WorkerCancelled,
    WorkerError,
    WorkerRegistry,
    WorkerRequest,
    WorkerResult,
)

__all__ = [
    "ACTIVE_TICKET_STATES", "AssignmentState", "AttentionNeed", "BudgetExhausted",
    "CompositeToolBox", "FallbackModelRunner", "HttpModelRunner",
    "InvestigationToolBox", "KnowledgeToolBox", "ModelRunnerError",
    "OdooReadToolBox", "PiJsonRunner", "PlanEffects", "RunnerUsage", "TOOL_SPECS", "TOOL_SPECS_BY_NAME",
    "ToolBox", "WebSearchToolBox", "parse_pi_usage", "specs_for", "tool_manifest",
    "investigation_tool_specs", "knowledge_tool_specs", "odoo_tool_specs",
    "web_tool_specs",
    "CallableWorker", "HeuristicRouterPolicy", "ModelReply", "ModelRouterPolicy",
    "ModelRunner", "RouterDirective", "RouterRequest", "ConditionSource", "ConditionSourceLoop", "ControlEngine",
    "ControlNotFound", "ControlStore", "Dispatcher", "DispatcherLoop", "DshWorker",
    "EffectKind", "EffectState", "EffectWatchdog", "FencingError",
    "KnowledgeGenerationSource", "LateResultRejected", "LeaseKeeper", "Mandate",
    "MandateDenied", "MandateScopeMode", "MandateState", "ObjectScope",
    "OdooPurchaseSource", "PiDockerRunner", "PiWorker", "PlanTask", "ProcessWatchdog", "RecoveryPolicy", "Role",
    "Router", "RouterDecision", "ScopeMode", "SideEffectClass", "ScanReport", "SourceEvent", "SourceScanner", "SourceStateStore",
    "OutboxConsumer", "OutboxError", "SourceLoop", "SourceRunner",
    "SourceService", "admit_events",
    "refresh_observed_state", "StalePolicy",
    "StateTransitionRejected", "StructuredModelWorker", "SubscriptionError",
    "Supervisor", "TERMINAL_TICKET_STATES", "TenantContext", "TenantError",
    "TicketState", "ToolLoopWorker", "ToolScope", "TriggerConflict",
    "USER_TICKET_STATE", "USER_TICKET_STATES",
    "WatchdogLoop", "WorkerCancelled", "WorkerError", "WorkerRegistry",
    "WorkerRequest", "WorkerResult",
    "answer_attention", "answer_attention_by_id", "approve_plan", "beat", "bind_odoo_user",
    "cancel_ticket", "health", "link_plan", "list_failure_attentions",
    "list_open_attentions", "list_plan_tasks", "list_subscriptions", "list_tickets",
    "normalize_ticket_state", "odoo_user_id_for", "open_attention", "phase_for",
    "plan_task",
    "principal_context", "reconcile_execution", "request_execution",
    "retry_failed_ticket", "revoke_approval", "role_for_principal",
    "ensure_subscription", "side_effect_for", "tenant_for_principal",
    "upsert_subscription", "user_state_for",
]

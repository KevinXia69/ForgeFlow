"""Router decision layer and tool-loop workers.

The previous ``Router`` was a capability dispatcher: it looked a string up in a
dict and called one worker once.  It is kept, underneath, as
:class:`WorkerRegistry`.  On top of it this module adds what the target design
actually asks for:

* a Router that produces a *schema-constrained decision* (answer, start an
  investigation, attach to an existing task, ask a clarifying question, prepare
  a plan, wait, stop, ignore) which the Control Engine then validates;
* workers that run a real tool loop against a host gateway instead of being told
  not to call tools;
* structured reports with separate observation/inference/unknown sections,
  evidence references, plan requests and provider-reported usage.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Iterable, Protocol
from uuid import uuid4

from psycopg.types.json import Jsonb

from .contracts import EffectKind, RouterDecision
from .identity import TenantContext, ToolScope
from .store import ControlStore


def specs_for(names):
    from .tools import specs_for as _specs_for

    return _specs_for(names)

#: A worker may not spend more than this many model turns on one assignment.
MAX_TOOL_STEPS = 8
#: Unknown tool names are a protocol error, never passed to a provider.
MAX_REPORT_FINDINGS = 200


class WorkerError(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class WorkerCancelled(WorkerError):
    def __init__(self, code: str = "worker_cancelled") -> None:
        super().__init__(code)


@dataclass(frozen=True, slots=True)
class WorkerRequest:
    tenant_id: str
    ticket_id: str
    goal: str
    input: dict[str, Any]
    capability: str
    budget_seconds: int = 120
    budget_tokens: int = 16000
    assignment_id: str = field(default_factory=lambda: str(uuid4()))
    mandate_id: str | None = None
    run_token: str | None = None
    object_ref: str | None = None
    #: What the user actually typed ("PO-123").  Resolving it to a record is the
    #: control layer's job; the router is not allowed to guess an id.
    object_reference: str | None = None
    #: Explicit tool restriction from the plan.  Empty means "whatever this worker
    #: can do": the worker's own tool set is already the security boundary and the
    #: per-object scope constrains which records are reachable.
    tools: tuple[str, ...] = ()
    inputs: tuple[dict[str, Any], ...] = ()
    principal_id: str | None = None

    def with_assignment(self, assignment_id: str) -> "WorkerRequest":
        return replace(self, assignment_id=assignment_id)


@dataclass(frozen=True, slots=True)
class WorkerResult:
    status: str
    summary: str
    findings: tuple[dict[str, Any], ...] = ()
    evidence: tuple[dict[str, Any], ...] = ()
    unknowns: tuple[str, ...] = ()
    attention: tuple[dict[str, Any], ...] = ()
    usage: dict[str, Any] = field(default_factory=dict)
    usage_complete: bool = False
    plan_request: dict[str, Any] | None = None
    next_effect: str | None = None
    decision: str | None = None

    def as_report(self, *, assignment_id: str, fencing_token: str | None,
                  ticket_id: str) -> dict[str, Any]:
        return {
            "task_id": ticket_id,
            "assignment_id": assignment_id,
            "fencing_token": fencing_token,
            "status": self.status,
            "summary": self.summary,
            "findings": list(self.findings),
            "evidence_refs": list(self.evidence),
            "unknowns": list(self.unknowns),
            "required_attention": list(self.attention),
            "usage": dict(self.usage),
            "usage_complete": bool(self.usage_complete),
            "plan_request": self.plan_request,
            "next_effect": self.next_effect,
            "decision": self.decision,
        }


class WorkerAdapter(Protocol):
    name: str
    capabilities: frozenset[str]

    def run(self, request: WorkerRequest) -> WorkerResult:
        ...


class CallableWorker:
    """Test/worker seam for a DSH or Pi process with a pre-bound runner."""

    def __init__(self, name: str, capabilities: set[str] | frozenset[str],
                 run: Callable[[WorkerRequest], WorkerResult]) -> None:
        self.name = name
        self.capabilities = frozenset(capabilities)
        self._run = run

    def run(self, request: WorkerRequest) -> WorkerResult:
        return self._run(request)


# --------------------------------------------------------------------- models
@dataclass(frozen=True, slots=True)
class ModelReply:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)
    usage_complete: bool = False


class ModelRunner(Protocol):
    """Host-provided model runner.  Workers never hold provider credentials."""

    def complete(self, prompt: str, *, run_token: str | None = None) -> ModelReply:
        ...


class CallableModelRunner:
    """Adapts a plain ``ask(prompt) -> str`` callable to :class:`ModelRunner`.

    Text-only runners cannot report token usage.  The result says so
    (``usage_complete=False``) instead of inventing a precise bill.
    """

    def __init__(self, ask: Callable[[str], str]) -> None:
        self._ask = ask

    def complete(self, prompt: str, *, run_token: str | None = None) -> ModelReply:
        del run_token
        return ModelReply(self._ask(prompt), {"tokens": len(prompt) // 4}, False)


# ---------------------------------------------------------------------- tools
class ToolBox(Protocol):
    """The only way a worker touches the outside world."""

    names: frozenset[str]

    def call(self, tool: str, arguments: dict[str, Any], *, scope: ToolScope) -> dict[str, Any]:
        ...


class RegistryToolBox:
    """Deterministic, host-wired tool set with no ambient credentials.

    Each entry receives ``(arguments, scope)`` and must itself respect the scope;
    the worker cannot widen it because it never sees the gateway state.
    """

    def __init__(self, tools: dict[str, Callable[[dict, ToolScope], dict]] | None = None,
                 *, allow_any: bool = False) -> None:
        self._tools = dict(tools or {})
        self._allow_any = bool(allow_any)

    @property
    def names(self) -> frozenset[str]:
        return frozenset(self._tools)

    def call(self, tool: str, arguments: dict[str, Any], *, scope: ToolScope) -> dict[str, Any]:
        if not scope.permits(tool):
            raise WorkerError(f"tool_not_permitted:{tool}")
        handler = self._tools.get(tool)
        if handler is None:
            raise WorkerError(f"tool_unavailable:{tool}")
        return handler(arguments or {}, scope)


# ------------------------------------------------------------------ reporting
def parse_worker_report(raw: str, *, usage: dict | None = None,
                        usage_complete: bool = False) -> WorkerResult:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no_json")
    payload = json.loads(raw[start:end + 1])
    status = str(payload.get("status", "failed"))
    if status == "insufficient_input":
        status = "needs_input"
    if status not in {"completed", "needs_input", "waiting_event", "failed"}:
        raise ValueError("invalid_status")
    findings = tuple(item for item in (payload.get("findings") or [])
                     if isinstance(item, dict))[:MAX_REPORT_FINDINGS]
    evidence = tuple(item for item in (payload.get("evidence") or [])
                     if isinstance(item, dict))[:MAX_REPORT_FINDINGS]
    unknowns = tuple(str(item) for item in (payload.get("unknowns") or []))
    attention = tuple(item for item in (payload.get("attention") or [])
                      if isinstance(item, dict))
    plan_request = payload.get("plan_request")
    if plan_request is not None and not isinstance(plan_request, dict):
        plan_request = None
    usage_value = dict(usage or {})
    return WorkerResult(
        status=status,
        summary=str(payload.get("summary", ""))[:4000],
        findings=findings,
        evidence=evidence,
        unknowns=unknowns,
        attention=attention,
        usage=usage_value,
        usage_complete=bool(usage_complete),
        plan_request=plan_request,
        next_effect=str(payload["next_effect"])[:64] if payload.get("next_effect") else None,
        decision=str(payload["decision"])[:64] if payload.get("decision") else None,
    )


REPORT_CONTRACT = (
    "Return ONE JSON object and nothing else, with keys:\n"
    '  "status": "completed" | "needs_input" | "waiting_event" | "failed"\n'
    '  "summary": short string for a business user\n'
    '  "findings": array of {"kind":"observation"|"inference"|"recommendation",'
    ' "statement":str, "source":str}\n'
    '  "evidence": array of {"source":str, "record":str, "field":str|null,'
    ' "version":str|null, "locator":str|null}\n'
    '  "unknowns": array of {"field":str, "object":str, "why_blocking":str}\n'
    '  "attention": array of {"need":"clarification"|"fact"|"approval"|"failure"|'
    '"rule"|"budget"|"result_unknown", "question":str}\n'
    '  "plan_request": {"objective":str, "snapshot_ref":str, "facts":object} | null\n'
    '  "next_effect": null\n'
    "Set plan_request only when the goal is to recover or change the business and you "
    "have observed the exact object; put the numeric record id you read (for example "
    '"purchase_order_id") in its facts. Otherwise return null.\n'
    "Never invent a fact you did not read. Separate what you observed from what you "
    "inferred. If a required fact is missing, use needs_input and name the exact field."
)


class StructuredModelWorker:
    """Single-shot summariser.

    Kept deliberately: it is useful for digesting supplied input.  It is *not* an
    investigation worker, because it is told not to call tools and cannot verify
    anything it writes.
    """

    summary_only = True

    def __init__(self, name: str, capabilities: set[str] | frozenset[str],
                 ask: Callable[[str], str], *, runner: ModelRunner | None = None) -> None:
        self.name = name
        self.capabilities = frozenset(capabilities)
        self._ask = ask
        self._runner = runner or CallableModelRunner(ask)

    def _prompt(self, request: WorkerRequest) -> str:
        return (
            "You are a read-only ForgeFlow summariser. Use only the input below; "
            "do not call tools and do not invent facts.\n"
            + REPORT_CONTRACT + "\nInput follows:\n"
            + json.dumps({"goal": request.goal, "input": request.input},
                         ensure_ascii=False)
        )

    def run(self, request: WorkerRequest) -> WorkerResult:
        try:
            reply = self._runner.complete(self._prompt(request),
                                          run_token=request.run_token)
            return parse_worker_report(reply.text, usage=reply.usage,
                                       usage_complete=reply.usage_complete)
        except (TypeError, ValueError, json.JSONDecodeError):
            return WorkerResult("failed", "worker_output_invalid",
                                unknowns=("invalid worker output",))


class DshWorker(StructuredModelWorker):
    """Host-injected DSH session runner (ask(prompt) -> text)."""


class PiWorker(StructuredModelWorker):
    """Host-injected Pi runner (ask(prompt) -> text)."""


#: Tool rejections caused by the *model's arguments*, not by an authorization
#: decision.  These become a bounded, visible observation instead of killing the
#: investigation; scope, permission and availability denials stay terminal.
RECOVERABLE_TOOL_ERRORS = frozenset({
    "tool_reference_model_mismatch",
    "tool_ids_required",
    "tool_fields_required",
    "tool_query_required",
    "tool_arguments_invalid",
})


def _recoverable_tool_error(code: str) -> bool:
    return str(code).split(":", 1)[0] in RECOVERABLE_TOOL_ERRORS


class ToolLoopWorker:
    """Investigation worker that actually calls authorised tools.

    Loop shape (plan section 6.2): read the target, identify missing facts, call
    an authorised tool, check sources and relations, revise the investigation
    plan, validate the draft, request planning, return a structured result.

    Every turn is bounded by ``max_steps``, by the ticket's token budget and by a
    cancellation check that is consulted *before* each model call and each tool
    call.  A stop therefore prevents the next tool invocation rather than waiting
    for the run to finish.
    """

    summary_only = False

    def __init__(self, name: str, capabilities: set[str] | frozenset[str], *,
                 runner: ModelRunner, tools: ToolBox, tool_specs: Iterable[dict] | None = None,
                 max_steps: int = MAX_TOOL_STEPS,
                 should_stop: Callable[[], bool] | None = None,
                 quotas: Any | None = None) -> None:
        if max_steps < 1:
            raise WorkerError("worker_invalid_max_steps")
        self.name = name
        self.capabilities = frozenset(capabilities)
        self._runner = runner
        self._tools = tools
        self._specs = (tuple(tool_specs) if tool_specs is not None
                       else specs_for(tools.names))
        self.max_steps = int(max_steps)
        self._should_stop = should_stop or (lambda: False)
        self._quotas = quotas

    # -- prompt construction -------------------------------------------------
    def _tool_manifest(self, scope: ToolScope) -> list[dict]:
        return [dict(spec) for spec in self._specs
                if spec.get("name") in self._tools.names
                and spec.get("name") in scope.allowed_tools]

    def _system(self, request: WorkerRequest, scope: ToolScope, *,
                final_turn: bool = False) -> str:
        closing = (
            "\nThis is the final turn. Do not call a tool. Reply with the final report "
            "JSON now; if the goal asks for a recovery, include plan_request with the "
            "numeric record id you observed (for example purchase_order_id).\n"
            if final_turn else "")
        return (
            "You are a read-only ForgeFlow business-investigation worker. You may call "
            "the tools listed below and nothing else. Every tool call is checked "
            "server-side against the task's mandate; a denied call is final and must "
            "not be retried with different arguments.\n"
            "Investigate iteratively: read the target, identify what fact is missing, "
            "call one tool, check the source and the business relationship, then revise. "
            "Stop when the goal is answered or when a fact cannot be obtained. Use at "
            "most three tool calls: after the first successful record read, prefer the "
            "final report over further exploration. If a tool rejects your arguments, "
            "correct them at most once; never repeat the same call.\n"
            + REPORT_CONTRACT + closing +
            "\nOn each turn reply with ONE JSON object:\n"
            '  {"action":"tool","tool":str,"arguments":object,"why":str}\n'
            "or\n"
            '  {"action":"final","report":{...the report object above...}}\n'
            "Available tools:\n"
            + json.dumps(self._tool_manifest(scope), ensure_ascii=False)
            + "\nTask:\n"
            + json.dumps({"goal": request.goal, "object_ref": request.object_ref,
                          "input": request.input,
                          "new_context": [item.get("body") for item in request.inputs]},
                         ensure_ascii=False)
        )

    # -- main loop -----------------------------------------------------------
    def run(self, request: WorkerRequest) -> WorkerResult:
        scope = ToolScope(
            tenant_id=request.tenant_id,
            principal_id=request.principal_id or request.tenant_id,
            ticket_id=request.ticket_id,
            assignment_id=request.assignment_id,
            mandate_id=request.mandate_id or "",
            capability=request.capability,
            allowed_tools=(frozenset(request.tools) & self._tools.names
                           if request.tools else self._tools.names),
            object_scope=_scope_for(request),
            budget_tokens=max(1, request.budget_tokens),
            expires_at=0.0,
            run_token=request.run_token or request.assignment_id,
        )
        transcript: list[dict] = []
        usage: dict[str, Any] = {"tokens": 0, "prompt_tokens": 0, "completion_tokens": 0}
        usage_complete = True
        evidence: list[dict] = []
        for step in range(self.max_steps):
            if self._should_stop():
                raise WorkerCancelled()
            prompt = self._system(
                request, scope, final_turn=step == self.max_steps - 1
            ) + "\nConversation so far:\n" + json.dumps(
                transcript, ensure_ascii=False)
            reply = self._runner.complete(prompt, run_token=request.run_token)
            usage, step_complete = _merge_usage(usage, reply.usage)
            usage_complete = usage_complete and step_complete
            if usage["tokens"] > request.budget_tokens:
                raise WorkerError("worker_token_budget_exceeded")
            action = _parse_action(reply.text)
            if action is None:
                return WorkerResult("failed", "worker_output_invalid",
                                    unknowns=("invalid worker action",),
                                    usage=usage, usage_complete=usage_complete)
            if action["action"] == "final":
                result = parse_worker_report(
                    json.dumps(action["report"], ensure_ascii=False), usage=usage,
                    usage_complete=usage_complete)
                if evidence:
                    result = replace(result, evidence=tuple(evidence) + result.evidence)
                return result
            tool, arguments = action["tool"], action.get("arguments") or {}
            if tool not in self._tools.names:
                raise WorkerError(f"tool_unavailable:{tool}")
            if self._quotas is not None:
                kinds = ["tool_calls"]
                if "search" in tool.lower():
                    kinds.append("search_calls")
                for kind in kinds:
                    decision = self._quotas.consume(request.tenant_id, kind, 1)
                    if not decision.get("allowed"):
                        return WorkerResult(
                            "needs_input", f"{kind}_quota_exhausted",
                            unknowns=(f"{kind} quota exhausted",),
                            attention=({"need": "budget",
                                        "question": ("The tenant or global tool/search "
                                                     "quota is exhausted.")},),
                            usage=usage, usage_complete=usage_complete)
            if self._should_stop():
                raise WorkerCancelled()
            try:
                observation = self._tools.call(tool, arguments, scope=scope)
            except WorkerError as error:
                # A *permission* denial is a terminal authorization result and must
                # not be retried with different arguments.  A malformed argument is
                # not a denial: the model may correct it once inside the same
                # bounded loop, with the error recorded as an observation.
                if _recoverable_tool_error(error.code):
                    transcript.append({
                        "step": step, "tool": tool, "arguments": arguments,
                        "why": str(action.get("why", ""))[:400],
                        "observation": {"status": "error", "error": error.code,
                                        "hint": ("The tool call was rejected because its "
                                                 "arguments were malformed. Correct the "
                                                 "arguments and retry once.")}})
                    continue
                return WorkerResult("failed", f"tool_denied:{error.code}",
                                    unknowns=(f"tool denied: {error.code}",),
                                    usage=usage, usage_complete=usage_complete)
            if tool not in scope.allowed_tools:
                raise WorkerError(f"tool_not_permitted:{tool}")
            evidence.append(_evidence_from(tool, observation))
            transcript.append({"step": step, "tool": tool, "arguments": arguments,
                               "why": str(action.get("why", ""))[:400],
                               "observation": _bounded_observation(observation)})
        return WorkerResult(
            "needs_input",
            "investigation_step_limit_reached",
            unknowns=("the investigation did not converge inside its step budget",),
            usage=usage, usage_complete=usage_complete)


def _evidence_from(tool: str, observation: dict) -> dict:
    """Build an evidence reference that points at something a person can open."""
    record = observation.get("record")
    reference = {
        "source": tool,
        "observation_id": observation.get("observation_id"),
        "record": record if isinstance(record, dict) else None,
        "field": None,
        "version": None,
        "locator": None,
    }
    if isinstance(record, dict):
        reference["locator"] = (record.get("locator") or record.get("observation_id")
                                or record.get("id"))
        reference["version"] = record.get("version") or record.get("document_version")
    return reference


def _scope_for(request: WorkerRequest):
    from .identity import ObjectScope

    if request.object_ref:
        return ObjectScope.of({request.object_ref})
    return ObjectScope.all_objects()


def _merge_usage(total: dict, extra: dict | None) -> tuple[dict, bool]:
    if not isinstance(extra, dict):
        return total, False
    complete = bool(extra.get("complete", "total_tokens" in extra))
    merged = dict(total)
    for key in ("tokens", "prompt_tokens", "completion_tokens"):
        merged[key] = int(merged.get(key, 0)) + int(extra.get(key, 0) or 0)
    if "total_tokens" in extra:
        merged["tokens"] = int(total.get("tokens", 0)) + int(extra.get("total_tokens") or 0)
    for key in ("model", "provider", "request_id"):
        if extra.get(key):
            merged[key] = extra[key]
    return merged, complete


def _parse_action(raw: str) -> dict | None:
    start, end = raw.find("{"), raw.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        payload = json.loads(raw[start:end + 1])
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    action = str(payload.get("action", "")).lower()
    if not action and isinstance(payload.get("status"), str) and isinstance(
            payload.get("summary"), str):
        # Some otherwise-compliant models return the report object directly.
        # Accept it only when it is shaped like a report; the report contract is
        # still validated by parse_worker_report.
        return {"action": "final", "report": payload}
    if action == "final":
        report = payload.get("report")
        return {"action": "final", "report": report} if isinstance(report, dict) else None
    if action == "tool":
        name = payload.get("tool")
        if not isinstance(name, str) or not name:
            return None
        arguments = payload.get("arguments")
        return {"action": "tool", "tool": name,
                "arguments": arguments if isinstance(arguments, dict) else {},
                "why": payload.get("why", "")}
    return None


def _bounded_observation(observation: dict, limit: int = 4000) -> dict:
    raw = json.dumps(observation, ensure_ascii=False, default=str)
    if len(raw) <= limit:
        return observation
    return {"truncated": True, "preview": raw[:limit]}


# --------------------------------------------------------------------- routing
@dataclass(frozen=True, slots=True)
class RouterRequest:
    tenant_id: str
    principal_id: str
    message: str
    object_ref: str | None = None
    #: What the user actually typed ("PO-123").  Resolving it to a record is the
    #: control layer's job; the router is not allowed to guess an id.
    object_reference: str | None = None
    ticket_id: str | None = None
    active_tickets: tuple[dict, ...] = ()
    goal_hint: str | None = None


@dataclass(frozen=True, slots=True)
class RouterDirective:
    """A Router *proposal*.  The Control Engine decides whether to accept it."""

    decision: str
    rationale: str = ""
    ticket_id: str | None = None
    object_ref: str | None = None
    #: What the user actually typed ("PO-123").  Resolving it to a record is the
    #: control layer's job; the router is not allowed to guess an id.
    object_reference: str | None = None
    goal: str | None = None
    capability: str | None = None
    question: str | None = None
    confidence: float = 0.0
    evidence: tuple[dict, ...] = ()

    def __post_init__(self) -> None:
        if self.decision not in {item.value for item in RouterDecision}:
            raise WorkerError(f"router_decision_invalid:{self.decision}")
        if self.decision == RouterDecision.REQUEST_CLARIFICATION.value and not self.question:
            raise WorkerError("router_clarification_question_required")
        if self.decision in {RouterDecision.START_INVESTIGATION.value,
                             RouterDecision.PREPARE_PLAN.value} and not self.goal:
            raise WorkerError("router_goal_required")


class RouterPolicy(Protocol):
    def decide(self, request: RouterRequest) -> RouterDirective:
        ...


class HeuristicRouterPolicy:
    """Deterministic policy used as a fallback and as a test oracle.

    It is intentionally conservative: it only starts work when the message names
    an object, and it attaches to an existing task when one is already running for
    that object.
    """

    #: Only an imperative counts as a stop. "the supplier cancelled PO-7" is a
    #: business fact about the world, not an instruction to the agent.
    _STOP_PATTERNS = (
        re.compile(r"^\s*(?:please\s+)?(?:stop|abort)\b", re.IGNORECASE),
        re.compile(r"\bstop\s+(?:the\s+)?(?:investigation|task|work|this|it)\b",
                   re.IGNORECASE),
        re.compile(r"\bcancel\s+(?:the\s+)?(?:task|investigation|work|this|it)\b",
                   re.IGNORECASE),
        re.compile(r"(?:停止|终止|取消)\s*(?:调查|任务|这件事|处理|分析)"),
        re.compile(r"别查了|不用查了|先别查"),
    )
    _WAIT = ("wait for now", "later", "等待", "以后再说", "稍后")
    _IGNORE = ("thanks", "thank you", "谢谢", "收到")

    def decide(self, request: RouterRequest) -> RouterDirective:
        message = (request.message or "").strip()
        lowered = message.casefold()
        if not message:
            return RouterDirective(RouterDecision.IGNORE.value, "empty_message", confidence=1.0)
        if any(pattern.search(message) for pattern in self._STOP_PATTERNS):
            return RouterDirective(RouterDecision.STOP_TASK.value, "stop_requested",
                                   ticket_id=request.ticket_id, confidence=0.8)
        if any(token in lowered for token in self._IGNORE) and len(message) < 40:
            return RouterDirective(RouterDecision.ANSWER.value, "acknowledgement",
                                   confidence=0.6)
        if any(token in lowered for token in self._WAIT):
            return RouterDirective(RouterDecision.WAIT_FOR_EVENT.value, "deferred_by_user",
                                   ticket_id=request.ticket_id, confidence=0.6)
        object_ref = request.object_ref or None
        object_reference = None if object_ref is not None else _object_ref_in(message)
        if object_ref is None and object_reference is None:
            return RouterDirective(
                RouterDecision.REQUEST_CLARIFICATION.value,
                "no_business_object_identified",
                question="Which order, purchase or manufacturing record should I look at?",
                confidence=0.4)
        wants_plan = any(token in lowered for token in ("plan", "方案", "恢复", "recover"))
        if object_ref is not None:
            existing = [item for item in request.active_tickets
                        if str(item.get("object_ref", "")) == object_ref]
            if existing:
                return RouterDirective(
                    RouterDecision.ATTACH_TO_EXISTING_TASK.value,
                    "object_already_in_progress",
                    ticket_id=str(existing[0].get("ticket_id") or existing[0].get("id")),
                    object_ref=object_ref, confidence=0.7)
            if wants_plan:
                return RouterDirective(RouterDecision.PREPARE_PLAN.value, "plan_requested",
                                       object_ref=object_ref,
                                       goal=request.goal_hint or message[:200],
                                       capability="plan", confidence=0.6)
            return RouterDirective(RouterDecision.START_INVESTIGATION.value,
                                   "object_identified", object_ref=object_ref,
                                   goal=request.goal_hint or message[:200],
                                   capability="assess", confidence=0.7)
        # A reference, not a record.  "PO-7" may be invisible to this user, or two
        # orders may share the name; only a lookup can say, so the router hands the
        # text to the control layer instead of inventing an id.
        if wants_plan:
            return RouterDirective(RouterDecision.PREPARE_PLAN.value, "plan_requested",
                                   object_reference=object_reference,
                                   goal=request.goal_hint or message[:200],
                                   capability="plan", confidence=0.5)
        return RouterDirective(RouterDecision.START_INVESTIGATION.value,
                               "reference_identified",
                               object_reference=object_reference,
                               goal=request.goal_hint or message[:200],
                               capability="assess", confidence=0.6)


class ModelRouterPolicy:
    """Schema-constrained Router reasoning through the host model runner."""

    def __init__(self, runner: ModelRunner, *, fallback: RouterPolicy | None = None) -> None:
        self._runner = runner
        self._fallback = fallback or HeuristicRouterPolicy()

    def _prompt(self, request: RouterRequest) -> str:
        return (
            "You route work for a manufacturing operations system. Decide what should "
            "happen with the incoming message. Reply with ONE JSON object:\n"
            '{"decision": one of ["answer","start_investigation",'
            '"attach_to_existing_task","request_clarification","prepare_plan",'
            '"wait_for_event","stop_task","ignore"], "rationale": str, '
            '"ticket_id": str|null, "object_ref": str|null, "goal": str|null, '
            '"capability": str|null, "question": str|null, "confidence": number}\n'
            "Rules: never approve or perform a business write; only propose work. "
            "Prefer attaching to an existing task for the same business object. "
            "Ask a clarifying question when no business object is identifiable.\n"
            "Context:\n"
            + json.dumps({"message": request.message, "object_ref": request.object_ref,
                          "ticket_id": request.ticket_id,
                          "active_tickets": list(request.active_tickets)[:20],
                          "goal_hint": request.goal_hint}, ensure_ascii=False)
        )

    def decide(self, request: RouterRequest) -> RouterDirective:
        try:
            reply = self._runner.complete(self._prompt(request))
            start, end = reply.text.find("{"), reply.text.rfind("}")
            if start < 0 or end <= start:
                raise ValueError("no_json")
            payload = json.loads(reply.text[start:end + 1])
            return RouterDirective(
                decision=str(payload["decision"]),
                rationale=str(payload.get("rationale", ""))[:500],
                ticket_id=(str(payload["ticket_id"]) if payload.get("ticket_id") else None),
                object_ref=(str(payload["object_ref"]) if payload.get("object_ref") else None),
                goal=(str(payload["goal"])[:500] if payload.get("goal") else None),
                capability=(str(payload["capability"])[:64]
                            if payload.get("capability") else None),
                question=(str(payload["question"])[:500] if payload.get("question") else None),
                confidence=float(payload.get("confidence") or 0.0))
        except Exception:
            return self._fallback.decide(request)


#: Business references the router may recognise in free text.
#:
#: A pattern either names a self-describing prefix (``PO-12``) or states the
#: model separately and lets the matched text stand as the record name.  The
#: text is always kept verbatim: ``P00046`` is the name Odoo stores, so turning
#: it into ``PO46`` makes an exact lookup miss the original order, or match a
#: different one.  The digits are not rewritten to a prettier number.
_OBJECT_PATTERNS: tuple[tuple[str, str | None], ...] = (
    (r"\b(PO[-/]?\d{1,9})\b", None),
    (r"\b(SO[-/]?\d{1,9})\b", None),
    (r"\b(MO[-/]?\d{1,9})\b", None),
    # Odoo's default purchase sequence: P followed by zero-padded digits.
    (r"\b(P0\d{3,})\b", "purchase.order"),
    (r"采购单\s*(\d+)", "purchase.order"),
    (r"销售订单\s*(\d+)", "sale.order"),
    (r"制造订单\s*(\d+)", "mrp.production"),
)


def _object_ref_in(message: str) -> str | None:
    """Return a model hint plus the reference exactly as the user wrote it.

    ``"P00046"`` becomes ``purchase.order:P00046``: the resolver splits the
    model from the name and searches by ``name``.  A plain prefix such as
    ``PO-12`` is returned unchanged, because it already names its model.
    """
    import re

    for pattern, model in _OBJECT_PATTERNS:
        match = re.search(pattern, message, re.IGNORECASE)
        if not match:
            continue
        raw = match.group(1)
        return raw if model is None else f"{model}:{raw}"
    return None


# --------------------------------------------------------------------- registry
class WorkerRegistry:
    """Capability -> worker lookup.  A capability string is a routing key only."""

    def __init__(self) -> None:
        self._workers: dict[str, WorkerAdapter] = {}

    def register(self, worker: WorkerAdapter) -> None:
        for capability in worker.capabilities:
            if capability in self._workers:
                raise WorkerError(f"capability_already_registered:{capability}")
            self._workers[capability] = worker

    def get(self, capability: str) -> WorkerAdapter | None:
        return self._workers.get(capability)

    @property
    def capabilities(self) -> frozenset[str]:
        return frozenset(self._workers)

    def __contains__(self, capability: object) -> bool:
        return capability in self._workers


class Router:
    """Decision layer over the registry; also keeps the historical dispatch path."""

    def __init__(self, store: ControlStore | None = None, *,
                 policy: RouterPolicy | None = None) -> None:
        self.store = store
        self.registry = WorkerRegistry()
        self.policy = policy

    @staticmethod
    def _resolve(reference: str, resolver) -> dict[str, Any]:
        """Look the reference up, or explain why no record can be named."""
        if resolver is None:
            return {"status": "unavailable", "reference": reference,
                    "question": (f"I need the {reference} record, but no business "
                                 "connection is configured.")}
        resolution = resolver.resolve(reference)
        if resolution.ambiguous:
            names = ", ".join(item.display_name for item in resolution.candidates[:3])
            return {"status": "ambiguous", "reference": reference,
                    "candidates": [item.as_dict() for item in resolution.candidates],
                    "question": f"{reference} matches more than one record: {names}. "
                                "Which one?"}
        if resolution.resolved is None:
            return {"status": "not_found", "reference": reference,
                    "question": f"I could not find {reference} in this company."}
        return {"status": "resolved", "ref": resolution.resolved.ref,
                "id": resolution.resolved.id,
                "display_name": resolution.resolved.display_name}

    # -- registry compatibility ---------------------------------------------
    def register(self, worker: WorkerAdapter) -> None:
        self.registry.register(worker)

    def dispatch(self, ctx: TenantContext, request: WorkerRequest, *,
                 record: bool = True) -> WorkerResult:
        """Run one worker once.

        ``record`` is for direct/test use, which lets the Router own the
        assignment row.  The dispatcher sets it to ``False`` because the Control
        Engine already started the assignment and will store the result itself,
        with the fencing token checked inside that write.
        """
        ctx.require_worker()
        ctx.assert_tenant(request.tenant_id)
        worker = self.registry.get(request.capability)
        if worker is None:
            raise WorkerError(f"capability_unavailable:{request.capability}")
        if request.budget_seconds <= 0 or request.budget_tokens <= 0:
            raise WorkerError("worker_budget_invalid")
        assignment_id = request.assignment_id
        if self.store is not None and record:
            with self.store.transaction() as cursor:
                cursor.execute(
                    "INSERT INTO control_assignments(id, tenant_id, ticket_id, runner, "
                    "capability, state, fencing_token, mandate_id) "
                    "VALUES (%s, %s, %s, %s, %s, 'running', %s, %s) "
                    "ON CONFLICT (id) DO NOTHING",
                    (assignment_id, ctx.tenant_id, request.ticket_id, worker.name,
                     request.capability, request.run_token,
                     request.mandate_id if request.mandate_id else None))
        try:
            result = worker.run(request)
        except Exception as error:
            if self.store is not None and record:
                with self.store.transaction() as cursor:
                    cursor.execute(
                        "UPDATE control_assignments SET state='failed', "
                        "error_code=%s, ceased_at=now(), updated_at=now() "
                        "WHERE id=%s AND tenant_id=%s",
                        (type(error).__name__, assignment_id, ctx.tenant_id))
            raise
        tokens = int((result.usage or {}).get("tokens", 0) or 0)
        if tokens > request.budget_tokens:
            raise WorkerError("worker_token_budget_exceeded")
        if self.store is not None and record:
            with self.store.transaction() as cursor:
                cursor.execute(
                    "UPDATE control_assignments SET state='completed', result_json=%s, "
                    "ceased_at=now(), updated_at=now() WHERE id=%s AND tenant_id=%s",
                    (Jsonb(result.as_report(assignment_id=assignment_id,
                                            fencing_token=request.run_token,
                                            ticket_id=request.ticket_id)),
                     assignment_id, ctx.tenant_id))
        return result

    # -- decision layer ------------------------------------------------------
    def decide(self, request: RouterRequest) -> RouterDirective:
        if self.policy is None:
            return HeuristicRouterPolicy().decide(request)
        return self.policy.decide(request)

    def apply(self, ctx: TenantContext, engine, request: RouterRequest,
              resolver=None) -> dict[str, Any]:
        """Translate a Router directive into a validated engine command.

        This is the boundary the plan requires: the Router proposes, the Control
        Engine checks identity, scope, task state and policy, and only then does
        anything change.
        """
        from .engine import MandateDenied, StateTransitionRejected

        directive = self.decide(request)
        outcome: dict[str, Any] = {"decision": directive.decision,
                                   "rationale": directive.rationale,
                                   "confidence": directive.confidence,
                                   "accepted": False}
        if directive.object_reference:
            resolved = self._resolve(directive.object_reference, resolver)
            outcome["resolution"] = resolved
            if resolved.get("status") != "resolved":
                # Without a record there is nothing to investigate; asking is
                # better than opening a task against an invented id.
                if request.ticket_id:
                    attention = engine.open_attention(
                        ctx, ticket_id=request.ticket_id, need_type="fact",
                        question=resolved.get("question")
                        or f"I could not find {directive.object_reference}.")
                    outcome.update(accepted=False, attention_id=attention,
                                   reason=resolved.get("status"))
                else:
                    outcome.update(accepted=False, question=resolved.get("question"),
                                   reason=resolved.get("status"))
                return outcome
            # Re-decide with the record in hand: attachment and planning are only
            # knowable once the reference names a real record.
            directive = self.decide(replace(request, object_ref=resolved["ref"]))
            outcome.update(decision=directive.decision,
                           rationale=directive.rationale,
                           confidence=directive.confidence)
        if directive.decision == RouterDecision.IGNORE.value:
            outcome["accepted"] = True
            return outcome
        if directive.decision == RouterDecision.ANSWER.value:
            outcome["accepted"] = True
            return outcome
        if directive.decision == RouterDecision.REQUEST_CLARIFICATION.value:
            if not request.ticket_id:
                outcome["accepted"] = True
                outcome["question"] = directive.question
                return outcome
            attention = engine.open_attention(
                ctx, ticket_id=request.ticket_id, need_type="clarification",
                question=str(directive.question))
            outcome.update(accepted=True, attention_id=attention)
            return outcome
        if directive.decision == RouterDecision.STOP_TASK.value:
            if not directive.ticket_id:
                raise StateTransitionRejected("stop_without_ticket")
            engine.cancel_ticket(ctx, ticket_id=directive.ticket_id,
                                 reason="router_stop")
            outcome.update(accepted=True, ticket_id=directive.ticket_id)
            return outcome
        if directive.decision == RouterDecision.WAIT_FOR_EVENT.value:
            if directive.ticket_id:
                engine.transition_ticket(ctx, ticket_id=directive.ticket_id,
                                         to_state="waiting_event", reason="router_wait")
                outcome.update(accepted=True, ticket_id=directive.ticket_id)
            else:
                outcome["accepted"] = True
            return outcome
        if directive.decision == RouterDecision.ATTACH_TO_EXISTING_TASK.value:
            if not directive.ticket_id:
                raise StateTransitionRejected("attach_without_ticket")
            input_id = engine.attach_input(
                ctx, ticket_id=directive.ticket_id,
                body={"message": request.message, "router": "attach"},
                kind="user_message", trust_level="untrusted")
            outcome.update(accepted=True, ticket_id=directive.ticket_id,
                           input_id=input_id)
            return outcome
        # start_investigation / prepare_plan both admit a new trigger.
        import hashlib
        import json as _json

        digest = hashlib.sha256(
            _json.dumps({"message": request.message,
                         "object": directive.object_ref},
                        sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        try:
            admission = engine.admit_trigger(
                ctx, source_id="router.workbench",
                external_event_id=f"workbench:{digest[:32]}",
                payload_hash=digest,
                object_ref=directive.object_ref or f"workbench:{request.principal_id}",
                goal=directive.goal or request.message[:200],
                goal_kind=directive.capability or "investigation",
                initial_effect_kind=EffectKind.LAUNCH_WORKER.value,
                capability=directive.capability or "assess",
                initial_payload={"capability": directive.capability or "assess",
                                 "input": {"message": request.message}})
        except (MandateDenied, StateTransitionRejected) as error:
            outcome.update(accepted=False, reason=getattr(error, "code", str(error)))
            return outcome
        outcome.update(accepted=True, ticket_id=admission.ticket_id,
                       duplicate=admission.duplicate, attached=admission.attached)
        return outcome


__all__ = [
    "CallableModelRunner", "CallableWorker", "DshWorker", "HeuristicRouterPolicy",
    "MAX_TOOL_STEPS", "ModelReply", "ModelRouterPolicy", "ModelRunner", "PiWorker",
    "REPORT_CONTRACT", "RegistryToolBox", "Router", "RouterDirective", "RouterPolicy",
    "RouterRequest", "StructuredModelWorker", "ToolBox", "ToolLoopWorker",
    "WorkerAdapter", "WorkerCancelled", "WorkerError", "WorkerRegistry", "WorkerRequest",
    "WorkerResult", "parse_worker_report",
]

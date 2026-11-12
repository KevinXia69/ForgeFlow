"""Host-only investigation handoff bindings and environment-wide dispatch guard.

Ordinary API clients never supply these bindings or filesystem paths. The host
obtains a verified binding from InvestigationCases and provides the factory
that starts an existing executor with this case's immutable request and policy.
"""
from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from forgeflow.contract import check_request, contract_from_payload
from forgeflow.investigation.event import trusted_event
from forgeflow.planning.live import validate_request
from forgeflow.recovery.storage import StorageError, atomic_json, digest, read_json, scope_lock

from .executor import ProductExecutionError

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
HEX32 = re.compile(r"[a-f0-9]{32}")
HEX64 = re.compile(r"[a-f0-9]{64}")
BINDING_FIELDS = {"owner", "generation", "source_case", "source_episode", "source_review_sha256",
                  "event", "request", "contract", "currency", "product_case_id"}
SUMMARY_FIELDS = {"binding_sha256", "generation", "source_case", "source_episode", "source_review_sha256",
                  "request_sha256", "policy_sha256", "task_id", "product_code", "sales_order_ids", "currency"}


def require(condition, reason="product_handoff_binding_changed"):
    if not condition:
        raise ProductExecutionError(reason)


REVIEW_PROFILE = "recovery-review-v1"


def _valid_fields(value, fields):
    return isinstance(value, dict) and (set(value) == fields or
        (set(value) == fields | {"review_profile"} and value["review_profile"] == REVIEW_PROFILE))


def validate_handoff(value, *, historical=False):
    fields = BINDING_FIELDS
    if isinstance(value, dict) and "confirmed_supply" in value:
        subject = value["confirmed_supply"]
        require(value.get("review_profile") == REVIEW_PROFILE and isinstance(subject, dict)
                and set(subject) == {"purchase_order_id", "purchase_line_id", "product_id", "supplier_id"}
                and all(type(v) is int and v > 0 for v in subject.values()), "invalid_product_handoff")
        fields = fields | {"confirmed_supply"}
    require(_valid_fields(value, fields), "invalid_product_handoff")
    value = copy.deepcopy(value)
    require(isinstance(value["owner"], str) and 0 < len(value["owner"]) <= 200,
            "invalid_product_handoff")
    require(all(isinstance(value[key], str) and IDENTIFIER.fullmatch(value[key])
                for key in ("generation", "source_case", "source_episode")), "invalid_product_handoff")
    require(isinstance(value["product_case_id"], str) and HEX32.fullmatch(value["product_case_id"])
            and isinstance(value["source_review_sha256"], str)
            and HEX64.fullmatch(value["source_review_sha256"]), "invalid_product_handoff")
    try:
        event = trusted_event(value["event"])
        request = validate_request(value["request"])
        contract = contract_from_payload(value["contract"])
        disruption = request["disruption"]
        if "purchase_order_id" in disruption and value.get("review_profile") == REVIEW_PROFILE:
            require(all(value.get("confirmed_supply", {}).get(key) == disruption[key]
                        for key in ("purchase_order_id", "purchase_line_id")), "invalid_product_handoff")
        checked = check_request(request, contract)
        expired = {issue["code"] for issue in checked["issues"]} == {"contract_expired"}
        require(event == value["event"] and request == value["request"]
                and contract.task_type == "supplier_cancellation"
                and (checked["ok"] or historical and expired), "product_handoff_contract_mismatch")
    except (KeyError, TypeError, ValueError) as error:
        raise ProductExecutionError("invalid_product_handoff") from error
    currency = value["currency"]
    require(isinstance(currency, dict) and isinstance(currency.get("currency"), str)
            and re.fullmatch(r"[A-Z]{3}", currency["currency"])
            and type(currency.get("company_id")) is int and currency["company_id"] > 0
            and isinstance(currency.get("provenance"), str) and currency["provenance"],
            "invalid_product_handoff_currency")
    return value


def handoff_summary(binding):
    contract = contract_from_payload(binding["contract"])
    return {"binding_sha256": digest(binding), "generation": binding["generation"],
            **({"review_profile": binding["review_profile"]} if "review_profile" in binding else {}),
            "source_case": binding["source_case"], "source_episode": binding["source_episode"],
            "source_review_sha256": binding["source_review_sha256"],
            "request_sha256": digest(binding["request"]), "policy_sha256": contract.digest(),
            "task_id": contract.task_id, "product_code": contract.product_code,
            "sales_order_ids": list(contract.sales_order_ids), "currency": binding["currency"]["currency"]}


def valid_summary(value):
    return (_valid_fields(value, SUMMARY_FIELDS)
        and all(isinstance(value[key], str) and HEX64.fullmatch(value[key]) for key in (
            "binding_sha256", "source_review_sha256", "request_sha256", "policy_sha256"))
        and all(isinstance(value[key], str) and IDENTIFIER.fullmatch(value[key]) for key in (
            "generation", "source_case", "source_episode", "task_id"))
        and isinstance(value["product_code"], str) and 0 < len(value["product_code"]) <= 512
        and isinstance(value["currency"], str) and re.fullmatch(r"[A-Z]{3}", value["currency"])
        and isinstance(value["sales_order_ids"], list) and bool(value["sales_order_ids"])
        and all(type(item) is int and item > 0 for item in value["sales_order_ids"])
        and len(set(value["sales_order_ids"])) == len(value["sales_order_ids"]))


@dataclass(frozen=True)
class HandoffPaths:
    directory: Path
    request: Path
    policy: Path
    store: Path
    approvals: Path
    socket: Path


class HandoffRegistry:
    """One host-owned registry for one ERP generation, shared across product stores."""

    def __init__(self, root, *, socket_root, generation, executor_factory):
        self.root = Path(root)
        self.socket_root = Path(socket_root)
        self.generation = generation
        self.executor_factory = executor_factory
        self._executors = {}

    def _root(self):
        for path in (self.root, self.root / "cases", self.socket_root):
            require(not path.is_symlink(), "product_handoff_store_untrusted")
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            require(path.is_dir(), "product_handoff_store_untrusted")

    def paths(self, binding):
        identity = binding["product_case_id"]
        require(isinstance(identity, str) and HEX32.fullmatch(identity), "invalid_product_handoff")
        directory = self.root / "cases" / identity
        token = digest({"case": identity, "generation": binding["generation"],
                        "binding": digest(binding)})[:32]
        # The existing daemon also creates executor.lock/executor.ready beside
        # its socket, so each case needs its own short socket directory.
        socket = self.socket_root / token / "executor.sock"
        require(len(str(socket).encode()) < 104, "product_handoff_socket_path_too_long")
        return HandoffPaths(directory, directory / "request.json", directory / "policy.json",
                            directory / "store", directory / "approvals", socket)

    def _environment(self):
        generation = self.generation()
        require(isinstance(generation, str) and IDENTIFIER.fullmatch(generation),
                "environment_generation_changed")
        path = self.root / "environment.json"
        if path.exists():
            state = read_json(path)
            require(isinstance(state, dict) and set(state) == {"schema_version", "generation", "pending", "last_closed"}
                    and state["schema_version"] == 1 and state["generation"] == generation,
                    "environment_generation_changed")
            pending = state["pending"]
            require(pending is None or (
                isinstance(pending, dict) and set(pending) == {"case_id", "plan_id", "owner_sha256", "phase"}
                and isinstance(pending["case_id"], str) and HEX32.fullmatch(pending["case_id"])
                and all(isinstance(pending[key], str) and HEX64.fullmatch(pending[key])
                        for key in ("plan_id", "owner_sha256"))
                and pending["phase"] in {"dispatching", "unknown"}), "environment_execution_unresolved")
        else:
            state = {"schema_version": 1, "generation": generation, "pending": None, "last_closed": None}
            atomic_json(path, state)
        return state

    def _save_environment(self, state):
        atomic_json(self.root / "environment.json", state, replace=True)

    @staticmethod
    def _same_file(path, value):
        require(not path.is_symlink(), "product_handoff_store_untrusted")
        if path.exists():
            require(read_json(path) == value)
        else:
            atomic_json(path, value)

    def register(self, binding):
        binding = validate_handoff(binding)
        self._root()
        with scope_lock(self.root):
            environment = self._environment()
            require(binding["generation"] == environment["generation"], "environment_generation_changed")
            require(environment["pending"] is None, "environment_execution_unresolved")
            paths = self.paths(binding)
            require(not paths.directory.is_symlink(), "product_handoff_store_untrusted")
            paths.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            require(not paths.socket.parent.is_symlink(), "product_handoff_store_untrusted")
            paths.socket.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            # Idempotent reconstruction after a host crash writes only the same
            # binding; it cannot replace any file of an existing product case.
            self._same_file(paths.directory / "binding.json", binding)
            self._same_file(paths.request, binding["request"])
            self._same_file(paths.policy, binding["contract"])
            return handoff_summary(binding)

    def binding(self, case, *, historical=False):
        self._root()
        identity = case.get("case_id")
        require(isinstance(identity, str) and HEX32.fullmatch(identity), "invalid_product_handoff")
        directory = self.root / "cases" / identity
        require(not directory.is_symlink(), "product_handoff_store_untrusted")
        binding = validate_handoff(read_json(directory / "binding.json"), historical=historical)
        require(binding["product_case_id"] == identity
                and case.get("_handoff") == handoff_summary(binding)
                and case.get("_owner_sha256") == hashlib.sha256(binding["owner"].encode()).hexdigest())
        require(binding["generation"] == self.generation(), "environment_generation_changed")
        paths = self.paths(binding)
        require(read_json(paths.request) == binding["request"] and read_json(paths.policy) == binding["contract"])
        return binding, paths

    def resolve(self, case, *, historical=False):
        binding, paths = self.binding(case, historical=historical)
        key = (binding["product_case_id"], digest(binding))
        if key not in self._executors:
            executor = self.executor_factory(copy.deepcopy(binding), paths)
            require(executor.request == binding["request"] and executor.store == paths.store
                    and executor.approvals == paths.approvals and executor.socket_path == str(paths.socket)
                    and getattr(executor, "handoff_sha256", None) == digest(binding),
                    "product_handoff_executor_mismatch")
            self._executors[key] = executor
        return self._executors[key]

    def closure(self, case):
        self._root()
        with scope_lock(self.root):
            state = self._environment()
            binding, _ = self.binding(case, historical=True)
            require(state["pending"] is None, "environment_execution_unresolved")
            return {"generation": binding["generation"], "environment_clear": True,
                    "environment_state_sha256": digest(state)}

    def invoke(self, method, case, args, kwargs, resolve):
        self._root()
        try:
            with scope_lock(self.root):
                state = self._environment()
                pending = state["pending"]
                plan = args[0] if args and isinstance(args[0], dict) else None
                same_pending = (isinstance(pending, dict) and pending.get("case_id") == case["case_id"]
                                and plan is not None and pending.get("plan_id") == plan.get("plan_id")
                                and pending.get("owner_sha256") == case.get("_owner_sha256"))
                # An unresolved dispatch contradicts an authorizing-only case.
                # Keep that environment held even when owner and plan match.
                closing = (method == "reconcile" or (method == "cancel_unstarted"
                           and (case.get("approval") or {}).get("phase") != "authorizing"))
                require(pending is None or closing and same_pending, "environment_execution_unresolved")
                executor = resolve(case)
                if method == "cancel_unstarted" and "allow_never_accepted" in kwargs:
                    if not getattr(executor, "supports_never_accepted_closure", False):
                        kwargs = {key: value for key, value in kwargs.items()
                                  if key != "allow_never_accepted"}
                if method == "apply":
                    require(plan is not None and isinstance(plan.get("plan_id"), str)
                            and HEX64.fullmatch(plan["plan_id"]), "product_handoff_plan_required")
                    state["pending"] = {"case_id": case["case_id"], "plan_id": plan["plan_id"],
                                        "owner_sha256": case.get("_owner_sha256"), "phase": "dispatching"}
                    self._save_environment(state)
                try:
                    result = getattr(executor, method)(case, *args, **kwargs)
                except BaseException:
                    if method == "apply":
                        state["pending"]["phase"] = "unknown"
                        self._save_environment(state)
                    raise
                known_execution = (method in {"apply", "reconcile"} and isinstance(result, dict)
                                   and isinstance(result.get("readback"), dict)
                                   and result["readback"].get("execution_validated") is True)
                known_unstarted = (method == "cancel_unstarted" and isinstance(result, dict)
                                   and result.get("outcome") == "not_started"
                                   and result.get("approval_invalidated") is True
                                   and result.get("plan_retired") is True)
                if state["pending"] is not None and (known_execution or known_unstarted):
                    state["last_closed"] = {**state["pending"], "receipt_sha256": digest(result)}
                    state["pending"] = None
                    self._save_environment(state)
                elif method == "apply":
                    state["pending"]["phase"] = "unknown"
                    self._save_environment(state)
                return result
        except StorageError as error:
            raise ProductExecutionError("environment_operation_unavailable", outcome_unknown=method == "apply") from error


class HandoffProductExecutor:
    """Resolve immutable local executors per case; optionally retain legacy A/B."""

    live = True
    supports_never_accepted_closure = True
    METHODS = frozenset({"prepare", "preparation_intent", "authorize", "apply", "reconcile",
                         "cancel_unstarted", "retire", "retire_comparison", "selection_intent",
                         "continue_selection", "validate_revision", "revision_intent", "continue_revision",
                         "repreparation_intent", "continue_repreparation", "revision_limits",
                         "review_sources", "check_deadline", "fact_revision_intent"})

    def __init__(self, registry, *, legacy=None):
        self.registry, self.legacy = registry, legacy
        self.supported_scenarios = tuple(dict.fromkeys(("A", *getattr(legacy, "supported_scenarios", ()))))
        self.supports_revisions = getattr(legacy, "supports_revisions", False)
        self.supports_candidates = getattr(legacy, "supports_candidates", False)

    def candidate_capable(self, case):
        handoff = case.get("_handoff")
        if handoff is not None:
            return handoff.get("review_profile") == REVIEW_PROFILE
        return self.supports_candidates

    def register_handoff(self, binding):
        return self.registry.register(binding)

    def handoff_closure(self, case):
        return self.registry.closure(case)

    def _resolve(self, case, *, historical=False):
        if case.get("_handoff") is not None:
            return self.registry.resolve(case, historical=historical)
        require(not (self.registry.root / "cases" / case["case_id"]).exists())
        require(self.legacy is not None, "product_scenario_unavailable")
        return self.legacy

    def can_execute_plan(self, case, plan):
        if case.get("_handoff") is not None:
            from .local import LocalProductExecutor
            binding, _ = self.registry.binding(case, historical=True)
            if not check_request(binding["request"], contract_from_payload(binding["contract"]))["ok"]:
                return False
            if binding.get("review_profile") == REVIEW_PROFILE and plan:
                # Read-only rendering must not start an executor. Older expanded
                # plans already expose their disrupted PO tokens in the saved projection.
                facts = (plan.get("impact_chain") or {}).get("source_facts") or {}
                scope = facts.get("disruption")
                if scope is not None:
                    tokens = scope.get("broken_tokens")
                    po = binding.get("confirmed_supply", {}).get("purchase_order_id")
                    cancellations = [op["entity_ref"] for op in facts.get("operations", [])
                                     if op["model"] == "purchase.order" and op["action"] == "cancel"]
                    if (not isinstance(tokens, list) or len(tokens) != 1 or tokens != cancellations
                            or po is not None and tokens != [f"existing:purchase.order:{po}"]):
                        return False
            return ((plan or {}).get("policy_sha256") == contract_from_payload(binding["contract"]).digest()
                    and LocalProductExecutor.can_execute_plan(case, plan))
        checker = getattr(self.legacy, "can_execute_plan", None)
        return checker(case, plan) if callable(checker) else self.legacy is not None

    def __getattr__(self, method):
        if method not in self.METHODS:
            raise AttributeError(method)
        def dispatch(case, *args, **kwargs):
            # Rendering existing state neither dispatches business work nor
            # starts a new executor for an unprepared handoff.
            if method == "revision_limits":
                if case.get("_handoff") is not None:
                    self.registry.binding(case, historical=True)
                    return None
                handler = getattr(self.legacy, method, None)
                return handler(case) if callable(handler) else None
            historical = method in {"retire", "retire_comparison", "review_sources", "reconcile", "cancel_unstarted"}
            return self.registry.invoke(method, case, args, kwargs,
                                        lambda value: self._resolve(value, historical=historical))
        return dispatch

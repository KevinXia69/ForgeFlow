"""Host-owned cases spanning immutable investigation episodes.

Authenticated adapters supply the principal; model tools cannot call mutation
methods. The single-file aggregate commits policy and continuation intent
together. Episode files are recoverable projections of that durable decision.
"""
from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from forgeflow.contract import check_request, contract_from_payload
from forgeflow.investigation.event import trusted_event
from forgeflow.investigation.intake import (
    IntakeError,
    compile_policy,
    inspect_intake,
    template_fields,
)
from forgeflow.investigation.policy import PartialPolicy, load_investigation_policy
from forgeflow.investigation.report import trusted_summary
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock

IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
CASE_ID = re.compile(r"ic-[a-f0-9]{32}")
RESOURCE_FIELDS = ("tokens", "seconds", "queries", "validations", "prepares")
DECISION_INPUT_MODES = {"legacy_reply", "structured_budget_v1"}


class CaseError(ValueError):
    def __init__(self, reason, status=409):
        super().__init__(reason)
        self.code, self.status = reason, status


def require(condition, reason, status=409):
    if not condition:
        raise CaseError(reason, status)


def identifier(value):
    require(isinstance(value, str) and IDENTIFIER.fullmatch(value), "invalid_operation_id", 400)
    return value


def amount_text(value):
    """Exact two-decimal case currency amounts; never silently round a grant."""
    require(not isinstance(value, bool) and isinstance(value, (str, int, float)),
            "invalid_budget_amount", 400)
    try:
        number = Decimal(str(value))
        require(number.is_finite() and 0 < number <= Decimal("1000000000000")
                and number == number.quantize(Decimal("0.01")), "invalid_budget_amount", 400)
    except InvalidOperation as error:
        raise CaseError("invalid_budget_amount", 400) from error
    return format(number.quantize(Decimal("0.01")), "f")


def structured_amount(value):
    """An exact ASCII decimal string, independently of legacy reply syntax."""
    require(isinstance(value, str) and 0 < len(value) <= 16
            and re.fullmatch(r"(?:0|[1-9][0-9]*)(?:\.[0-9]{1,2})?", value),
            "invalid_budget_amount", 400)
    number = Decimal(value)
    require(0 < number <= Decimal("1000000000000"), "invalid_budget_amount", 400)
    return format(number.quantize(Decimal("0.01")), "f")


@dataclass(frozen=True)
class CaseLimits:
    # Two high-budget investigations plus reply interpretation; not a reset of
    # the original 2M allowance on every click or child-process launch.
    tokens: int = 6_000_000
    seconds: int = 10_800
    queries: int = 240
    validations: int = 72
    prepares: int = 9
    replies: int = 12
    children: int = 1

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()) or self.children != 1:
            raise ValueError("invalid_case_limits")


class InvestigationCases:
    def __init__(self, root: Path, *, source_factory, generation, currency_probe,
                 clock=time.time):
        self.root = Path(root)
        self.source_factory = source_factory
        self.generation = generation
        self.currency_probe = currency_probe
        self.clock = clock

    def _directory(self, case_id):
        require(isinstance(case_id, str) and CASE_ID.fullmatch(case_id), "invalid_case_id", 400)
        path = self.root / case_id
        require(not path.is_symlink(), "invalid_case_directory")
        return path

    def _read(self, case_id, principal):
        try:
            state = read_json(self._directory(case_id) / "case.json")
        except FileNotFoundError as error:
            raise CaseError("case_not_found", 404) from error
        require(state["owner"] == principal, "case_not_found", 404)
        return state

    def _write(self, state):
        atomic_json(self._directory(state["case_id"]) / "case.json", state, replace=True)

    def _current(self, state):
        self._decision_binding(state)
        require(not state.get("environment_released"), "case_environment_released")
        require(not state.get("closure_intent"), "case_closing")
        require(not state.get("runtime_failure"), "case_runtime_rejected")
        require(self.generation() == state["generation"], "environment_generation_changed")
        template_fields(state["template"], load_investigation_policy(state["parent_policy"]), self.clock())
        require(not any(row["phase"] == "unknown" for row in state["work"].values()),
                "case_usage_unknown")

    @staticmethod
    def _decision_binding(state):
        mode = state.get("decision_input_mode", "legacy_reply")
        require(isinstance(mode, str) and mode in DECISION_INPUT_MODES,
                "decision_contract_mismatch")
        version = state.get("decision_contract_version")
        require(type(version) is int and version == 1 if mode == "structured_budget_v1"
                else version is None, "decision_contract_mismatch")
        return mode

    @classmethod
    def _legacy_reply(cls, state):
        require(cls._decision_binding(state) == "legacy_reply", "structured_budget_required")

    @staticmethod
    def _not_paused(state):
        require(not state.get("decision_paused", False), "case_decision_paused")

    def _currency(self, template):
        evidence = copy.deepcopy(self.currency_probe(template))
        require(isinstance(evidence, dict)
                and set(evidence) == {"currency", "company_id", "record_currencies", "provenance"}
                and isinstance(evidence["currency"], str)
                and re.fullmatch(r"[A-Z]{3}", evidence["currency"])
                and isinstance(evidence["provenance"], str) and evidence["provenance"]
                and type(evidence["company_id"]) is int and evidence["company_id"] > 0
                and isinstance(evidence["record_currencies"], dict)
                and evidence["record_currencies"]
                and all(value == evidence["currency"] for value in evidence["record_currencies"].values()),
                "single_currency_unproven")
        return evidence

    def _operation(self, state, operation_id, payload, expected_revision):
        identifier(operation_id)
        prior = state["operations"].get(operation_id)
        if prior:
            require(prior["input_sha256"] == digest(payload), "operation_input_conflict")
            return copy.deepcopy(prior["receipt"])
        require(type(expected_revision) is int and expected_revision == state["revision"],
                "case_revision_conflict")
        return None

    def _commit(self, state, operation_id, payload, receipt):
        state["revision"] += 1
        state["updated_at"] = self.clock()
        receipt = {**receipt, "case_id": state["case_id"], "revision": state["revision"]}
        state["operations"][operation_id] = {"input_sha256": digest(payload), "receipt": receipt}
        self._write(state)
        return copy.deepcopy(receipt)

    def public(self, state):
        keys = ("case_id", "generation", "revision", "status", "created_at", "updated_at",
                "current_episode", "limits", "ticket", "draft", "confirmation", "handoff",
                "terminal", "replies")
        view = {key: copy.deepcopy(state[key]) for key in keys}
        view["currency"] = state["currency"]["currency"]
        view["environment_released"] = copy.deepcopy(state.get("environment_released"))
        view["reply_contract_version"] = state.get("reply_contract_version", 1)
        view["decision_input_mode"] = state.get("decision_input_mode", "legacy_reply")
        view["decision_contract_version"] = state.get("decision_contract_version")
        view["decision_paused"] = bool(state.get("decision_paused", False))
        view["budget_inputs"] = copy.deepcopy(state.get("budget_inputs", []))
        view["reply_outcome"] = copy.deepcopy(state.get("reply_outcome"))
        view["draft_history"] = copy.deepcopy(state.get("draft_history", []))
        expiry = state["template"]["fields"].get("expires_at_utc")
        view["policy_expired"] = expiry is not None and datetime.fromisoformat(expiry).timestamp() <= self.clock()
        closure = state.get("closure_intent")
        view["closure_intent"] = ({key: copy.deepcopy(closure.get(key)) for key in
            ("operation_id", "reason", "requested_at", "phase", "blockers", "receipt")}
            if closure else None)
        view["episodes"] = [{key: row[key] for key in
                            ("episode_id", "parent_episode", "policy_sha256", "status")}
                            for row in state["episodes"].values()]
        known = {field: sum(row.get("usage", {}).get(field, 0) or 0
                            for row in state["work"].values()) for field in RESOURCE_FIELDS}
        view["known_usage"] = known
        view["usage"] = {field: None if any(row["phase"] == "reserved"
                           or row.get("usage", {}).get(field) is None for row in state["work"].values())
                         else known[field] for field in RESOURCE_FIELDS}
        view["usage_complete"] = all(row["phase"] == "complete" for row in state["work"].values())
        return view

    def get(self, case_id, principal):
        with scope_lock(self.root):
            return self.public(self._read(case_id, principal))

    def list(self, principal):
        """Return only this authenticated owner's cases; never host paths."""
        with scope_lock(self.root):
            found = []
            for directory in self.root.iterdir():
                if not CASE_ID.fullmatch(directory.name) or directory.is_symlink() or not directory.is_dir():
                    continue
                try:
                    state = read_json(directory / "case.json")
                    if state.get("owner") == principal:
                        found.append(self.public(state))
                except (OSError, ValueError, RuntimeError, KeyError, TypeError):
                    # One unreadable case must not expose another owner's file
                    # or prevent the authenticated owner listing intact cases.
                    continue
            return sorted(found, key=lambda row: row["updated_at"], reverse=True)

    def create(self, principal, *, operation_id, template, policy, event, limits=None,
               reply_contract_version=1, decision_input_mode="legacy_reply"):
        """Trusted setup only: template/policy/currency never come from reply JSON."""
        require(isinstance(principal, str) and 0 < len(principal) <= 200, "invalid_principal", 400)
        identifier(operation_id)
        require(type(reply_contract_version) is int and reply_contract_version in {1, 2},
                "invalid_reply_contract_version", 400)
        require(isinstance(decision_input_mode, str) and decision_input_mode in DECISION_INPUT_MODES,
                "invalid_decision_input_mode", 400)
        require(isinstance(policy, PartialPolicy), "partial_policy_required", 400)
        event = trusted_event(event)
        limits = limits or CaseLimits()
        fields = template_fields(template, policy, self.clock())
        require(sum(r["budget_cap"] is None for r in policy.payload()["orders"].values()) == 1,
                "single_missing_budget_required")
        case_id = "ic-" + digest({"owner": principal, "operation_id": operation_id})[:32]
        generation = self.generation()
        identifier(generation)
        currency = self._currency(template)
        inputs = {"owner": principal, "template": template, "policy": policy.payload(),
                  "event": event, "generation": generation, "currency": currency, "limits": asdict(limits)}
        # Preserve admission hashes of existing v1 aggregates.
        if reply_contract_version != 1:
            inputs["reply_contract_version"] = reply_contract_version
        if decision_input_mode == "structured_budget_v1":
            inputs.update(decision_input_mode=decision_input_mode, decision_contract_version=1)
        with scope_lock(self.root):
            directory = self._directory(case_id)
            if (directory / "case.json").exists():
                previous = self._read(case_id, principal)
                require(previous["input_sha256"] == digest(inputs), "operation_input_conflict")
                self._materialize(previous)
                return self.public(previous)
            environment_path = self.root / "environment.json"
            environment = read_json(environment_path) if environment_path.exists() else None
            if environment:
                require(environment["generation"] == generation, "environment_generation_changed")
                require(environment["active_case"] in {None, case_id}, "environment_chain_active")
                require(not environment.get("write_operation"), "environment_execution_unresolved")
            # Claim first. A crash before aggregate creation can only be resumed
            # by this deterministic case identity, not by a different operation.
            atomic_json(environment_path, {"generation": generation, "active_case": case_id,
                        "write_operation": None}, replace=environment_path.exists())
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            episode_id = case_id + "-e1"
            stamp = self.clock()
            state = {"schema_version": 1, "case_id": case_id, "owner": principal,
                     "reply_contract_version": reply_contract_version,
                     "input_sha256": digest(inputs), "generation": generation,
                     "template": copy.deepcopy(template), "template_sha256": digest(template),
                     "parent_policy": policy.payload(), "event": event, "currency": currency,
                     "status": "investigating", "revision": 1, "created_at": stamp, "updated_at": stamp,
                     "limits": asdict(limits), "current_episode": episode_id,
                     "episodes": {episode_id: {"episode_id": episode_id, "parent_episode": None,
                         "policy": policy.payload(), "policy_sha256": policy.digest(), "status": "pending"}},
                     "work": {}, "ticket": None, "draft": None, "replies": [], "confirmation": None,
                     "continuation": None, "handoff": None, "terminal": None, "operations": {},
                     "scope": fields["sales_order_ids"]}
            if decision_input_mode == "structured_budget_v1":
                state.update(decision_input_mode=decision_input_mode, decision_contract_version=1,
                             decision_paused=False, budget_inputs=[])
            self._write(state)
            self._materialize(state)
            return self.public(state)

    def _start_closure(self, state, *, operation_id, reason, expected_revision):
        if not state.get("closure_intent"):
            state["closure_intent"] = {"schema_version": 1, "operation_id": operation_id,
                "input_sha256": digest({"reason": reason, "revision": expected_revision}),
                "case_id": state["case_id"], "owner": state["owner"],
                "generation": state["generation"], "expected_revision": expected_revision,
                "reason": reason, "requested_at": self.clock(), "phase": "requested",
                "blockers": [], "receipt": None}
            state["status"] = "closing"

    def request_closure(self, case_id, principal, *, operation_id, expected_revision,
                        reason="user_cancelled"):
        """Authenticated host closure; expiry cannot invalidate historical receipts."""
        require(reason in {"user_cancelled", "policy_expired"}, "invalid_closure_reason", 400)
        payload = {"kind": "closure", "revision": expected_revision, "reason": reason}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            prior = self._operation(state, operation_id, payload, expected_revision)
            if prior:
                return prior
            require(self.generation() == state["generation"], "environment_generation_changed")
            require(not state.get("environment_released"), "case_environment_released")
            if reason == "policy_expired":
                try:
                    template_fields(state["template"], load_investigation_policy(state["parent_policy"]), self.clock())
                except IntakeError as error:
                    require(str(error) == "policy_expired", "closure_expiry_unproven")
                else:
                    raise CaseError("closure_expiry_unproven")
            self._start_closure(state, operation_id=operation_id, reason=reason,
                                expected_revision=expected_revision)
            return self._commit(state, operation_id, payload,
                                {"closure_operation_id": state["closure_intent"]["operation_id"]})

    def closure_blocked(self, case_id, principal, reason):
        """Host-only finite reasons; never store exception text or private paths."""
        allowed = {"worker_result_unknown", "case_usage_unknown", "product_handoff_not_closed",
                   "environment_execution_unresolved", "environment_generation_changed",
                   "product_handoff_unknown", "host_evidence_unavailable"}
        require(reason in allowed, "invalid_closure_blocker")
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            intent = state.get("closure_intent")
            require(intent is not None, "closure_not_requested")
            if intent["phase"] != "closed" and intent.get("blockers") != [reason]:
                intent.update(phase="reconciliation_required", blockers=[reason])
                state["revision"] += 1
                self._write(state)
            return self.public(state)

    def dispatch_worker(self, case_id, principal, *, operation_id):
        """Linearize dispatch against closure under the aggregate lock."""
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            require(operation_id in state["work"], "work_not_found", 404)
            row = state["work"][operation_id]
            require(row["phase"] == "reserved" and not row.get("dispatch"), "worker_already_dispatched")
            try:
                self._current(state)
                self._not_paused(state)
            except (CaseError, IntakeError) as error:
                reason = str(error)
                require(reason in {"case_closing", "policy_expired", "environment_generation_changed",
                                   "case_environment_released", "case_runtime_rejected", "case_decision_paused"}, reason)
                decision = {"authorized": False, "reason": reason}
            else:
                decision = {"authorized": True, "reason": None}
            row["dispatch"] = {**decision, "at": self.clock()}
            state["revision"] += 1
            self._write(state)
            return decision

    def _receipt_current(self, state):
        """Original callback identity is checked without renewing business authority."""
        require(self.generation() == state["generation"], "environment_generation_changed")
        if state.get("closure_intent"):
            return
        try:
            self._current(state)
        except IntakeError as error:
            if str(error) != "policy_expired":
                raise
            self._start_closure(state, operation_id="expiry-receipt-" + str(state["revision"]),
                                reason="policy_expired", expected_revision=state["revision"])
            state["revision"] += 1
            self._write(state)

    def _materialize(self, state):
        for episode_id, episode in state["episodes"].items():
            directory = self._directory(state["case_id"]) / "episodes" / episode_id
            require(not directory.is_symlink(), "invalid_episode_directory")
            directory.mkdir(mode=0o700, parents=True, exist_ok=True)
            for name, value in {"policy.json": episode["policy"], "event.json": state["event"]}.items():
                path = directory / name
                if path.exists():
                    require(read_json(path) == value, "episode_binding_changed")
                else:
                    atomic_json(path, value)
            (directory / "store").mkdir(mode=0o700, exist_ok=True)

    def episode_config(self, case_id, principal, episode_id):
        """Host-only paths, never projected into a model's ticket/context."""
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._current(state)
            require(episode_id in state["episodes"], "episode_not_found", 404)
            require(self._currency(state["template"]) == state["currency"], "currency_binding_changed")
            self._materialize(state)
            directory = self._directory(case_id) / "episodes" / episode_id
            return {"run_id": episode_id, "store": directory / "store",
                    "policy_file": directory / "policy.json", "event_file": directory / "event.json",
                    "socket": directory / "executor.sock", "output": directory / "output",
                    "policy": load_investigation_policy(state["episodes"][episode_id]["policy"])}

    def record_terminal(self, case_id, principal, episode_id):
        """Read the trusted service store, not an agent's final text or status."""
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._receipt_current(state)
            require(episode_id == state["current_episode"], "episode_binding_changed")
            episode = state["episodes"][episode_id]
            require(not any(row["phase"] == "reserved" for row in state["work"].values()),
                    "worker_still_active")
            path = self._directory(case_id) / "episodes" / episode_id / "store" / "investigation" / "state.json"
            terminal = read_json(path)
            require(terminal["run_id"] == episode_id and terminal["policy_sha256"] == episode["policy_sha256"]
                    and terminal["event_sha256"] == state["event"]["sha256"], "episode_binding_changed")
            require(terminal["status"] in {"prepared", "needs_clarification", "infeasible", "unsupported",
                                          "technical_failure", "budget_exhausted"}, "episode_not_terminal")
            if episode["status"] in {"prepared", "needs_clarification", "infeasible", "unsupported",
                                     "technical_failure", "budget_exhausted"}:
                require(episode["terminal_sha256"] == digest(terminal), "episode_terminal_changed")
                return self.public(state)
            episode.update(status=terminal["status"], terminal_sha256=digest(terminal))
            if state.get("closure_intent"):
                state["revision"] += 1
                self._write(state)
                return self.public(state)
            if terminal["status"] == "needs_clarification" and episode["parent_episode"] is None:
                policy = load_investigation_policy(state["parent_policy"])
                try:
                    proof = inspect_intake(self.source_factory(policy), template=state["template"],
                                           policy=policy, event=state["event"], now=self.clock())
                except IntakeError as error:
                    # Preserve the parent's actual question and terminal bytes.
                    # An unsupported intake must not leave the UI investigating
                    # forever or manufacture a budget ticket for another need.
                    state["status"] = "stopped"
                    state["terminal"] = {"status": "unsupported", "reason": str(error)}
                    state["revision"] += 1
                    self._write(state)
                    return self.public(state)
                state["ticket"] = {"ticket_id": "ticket-" + digest(proof)[:32],
                    "parent_episode": episode_id, "parent_policy_sha256": policy.digest(),
                    "order_id": proof["order_id"], "order_name": proof["order_name"],
                    "kind": "set_missing_order_budget", "facts_sha256": proof["facts_sha256"],
                    "question": f"What budget do you authorize for {proof['order_name']} ({state['currency']['currency']})?",
                    "model_question": terminal["terminal"].get("question"), "status": "open"}
                state["intake_proof"] = proof
                state["status"] = "awaiting_human"
            elif terminal["status"] == "prepared":
                state["status"] = "ready_for_review"
            else:
                state["status"] = "stopped"
                state["terminal"] = {"status": terminal["status"], **terminal["terminal"]}
            state["revision"] += 1
            self._write(state)
            return self.public(state)

    def _structured_waiting(self, state):
        self._current(state)
        require(self._decision_binding(state) == "structured_budget_v1", "structured_budget_mode_required")
        require(state["status"] == "awaiting_human" and state["confirmation"] is None
                and state["ticket"] is not None and state["ticket"]["status"] == "open",
                "case_not_waiting")
        require(not any(row["phase"] == "reserved" for row in state["work"].values()),
                "worker_still_active")

    def _decision_context_current(self, state):
        require(self._currency(state["template"]) == state["currency"], "currency_binding_changed")
        policy = load_investigation_policy(state["parent_policy"])
        proof = inspect_intake(self.source_factory(policy), template=state["template"],
                               policy=policy, event=state["event"], now=self.clock())
        require(proof == state["intake_proof"], "ticket_facts_changed")
        self._current(state)
        return policy, proof

    @staticmethod
    def _structured_candidate(state, draft):
        """Match the saved candidate to its exact authenticated structured input."""
        require(draft.get("source") == "structured_budget"
                and draft.get("decision_input_mode") == "structured_budget_v1"
                and draft.get("decision_contract_version") == 1
                and digest({key: value for key, value in draft.items() if key != "draft_sha256"}) == draft["draft_sha256"],
                "draft_not_confirmable")
        inputs = state.get("budget_inputs", [])
        require(bool(inputs), "draft_not_confirmable")
        row = inputs[-1]
        binding_keys = ("raw_input", "actor", "source", "decision_input_mode",
                        "decision_contract_version", "context_sha256")
        require(all(key in row for key in binding_keys), "draft_not_confirmable")
        require(row.get("status") == "candidate" and row.get("input_id") == draft.get("input_id")
                and row.get("actor") == state["owner"] and row.get("source") == "authenticated_user"
                and row.get("decision_input_mode") == state["decision_input_mode"]
                and row.get("decision_contract_version") == state["decision_contract_version"]
                and row.get("context_sha256") == draft["context_sha256"]
                and row.get("input_sha256") == draft.get("input_sha256")
                and digest({key: row[key] for key in binding_keys}) == row["input_sha256"],
                "draft_not_confirmable")
        raw = row["raw_input"]
        require(isinstance(raw, dict) and set(raw) == {"ticket_id", "amount"}
                and raw["ticket_id"] == state["ticket"]["ticket_id"], "draft_not_confirmable")
        canonical = structured_amount(raw["amount"])
        require(row.get("canonical_amount") == canonical and draft["proposal"] == {
            "kind": "budget_authorization_proposal", "order_id": state["ticket"]["order_id"],
            "currency": state["currency"]["currency"], "amount": canonical}, "draft_not_confirmable")
        return row

    def _invalidate_budget(self, state, *, operation_id, reason, replacement=None):
        draft = state["draft"]
        if draft is None:
            return
        row = self._structured_candidate(state, draft)
        invalidation = {"invalidation_reason": reason, "invalidated_at": self.clock(),
                        "invalidated_by_operation_id": operation_id,
                        "superseded_by_input_id": replacement}
        row.update(status="invalidated", **invalidation)
        state.setdefault("draft_history", []).append({"input_id": draft["input_id"],
            "source": draft["source"], "draft_sha256": draft["draft_sha256"],
            **{key: draft["proposal"][key] for key in ("order_id", "amount", "currency")}, **invalidation})
        state["draft"] = None

    def submit_budget(self, case_id, principal, *, operation_id, expected_revision, ticket_id, amount):
        """Save a typed user candidate; no language parser, policy grant or dispatch."""
        canonical = structured_amount(amount)
        require(isinstance(ticket_id, str) and 0 < len(ticket_id) <= 120, "invalid_ticket_id", 400)
        raw = {"ticket_id": ticket_id, "amount": amount}
        payload = {"kind": "structured_budget", "revision": expected_revision, **raw}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            prior = self._operation(state, operation_id, payload, expected_revision)
            if prior:
                return prior
            self._structured_waiting(state)
            self._not_paused(state)
            require(ticket_id == state["ticket"]["ticket_id"], "ticket_binding_changed")
            require(len(state["budget_inputs"]) < state["limits"]["replies"], "budget_input_limit")
            self._decision_context_current(state)
            input_id = "budget-" + digest({"case_id": case_id, "operation_id": operation_id, "payload": payload})[:32]
            binding = {"raw_input": raw, "actor": principal, "source": "authenticated_user",
                       "decision_input_mode": "structured_budget_v1", "decision_contract_version": 1,
                       "context_sha256": self._draft_context(state)}
            input_sha256 = digest(binding)
            self._invalidate_budget(state, operation_id=operation_id, reason="replaced",
                                    replacement=input_id)
            state["budget_inputs"].append({**binding, "input_id": input_id, "input_sha256": input_sha256,
                "canonical_amount": canonical, "received_at": self.clock(), "status": "candidate"})
            draft = {"source": "structured_budget", "input_id": input_id, "input_sha256": input_sha256,
                     "decision_input_mode": "structured_budget_v1", "decision_contract_version": 1,
                     "proposal": {"kind": "budget_authorization_proposal", "order_id": state["ticket"]["order_id"],
                                  "amount": canonical, "currency": state["currency"]["currency"]},
                     "parent_policy_sha256": state["ticket"]["parent_policy_sha256"],
                     "context_sha256": self._draft_context(state)}
            draft["draft_sha256"] = digest(draft)
            state["draft"] = draft
            return self._commit(state, operation_id, payload,
                                {"input_id": input_id, "draft_sha256": draft["draft_sha256"]})

    def budget_control(self, case_id, principal, *, operation_id, expected_revision, action):
        """Explicit candidate/wait controls; never cancel the case or authorize a cap."""
        require(isinstance(action, str) and action in {"withdraw", "pause", "resume"},
                "invalid_budget_control", 400)
        payload = {"kind": "budget_control", "revision": expected_revision, "action": action}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            prior = self._operation(state, operation_id, payload, expected_revision)
            if prior:
                return prior
            self._structured_waiting(state)
            if action == "resume":
                require(state.get("decision_paused") is True, "case_not_paused")
                require(state.get("pause_context_sha256") == self._draft_context(state), "ticket_binding_changed")
                self._decision_context_current(state)
                state["decision_paused"] = False
            else:
                self._not_paused(state)
                self._invalidate_budget(state, operation_id=operation_id, reason=action)
                if action == "pause":
                    state["decision_paused"] = True
                    state["pause_context_sha256"] = self._draft_context(state)
            return self._commit(state, operation_id, payload,
                                {"action": action, "decision_paused": state["decision_paused"]})

    def submit_reply(self, case_id, principal, *, operation_id, expected_revision, text):
        require(isinstance(text, str) and 0 < len(text.encode()) <= 8000, "invalid_reply", 400)
        payload = {"kind": "reply", "revision": expected_revision, "text": text}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            prior = self._operation(state, operation_id, payload, expected_revision)
            if prior:
                return prior
            self._legacy_reply(state)
            self._current(state)
            require(state["status"] == "awaiting_human", "case_not_waiting")
            require(len(state["replies"]) < state["limits"]["replies"], "reply_limit")
            reply = {"reply_id": "reply-" + digest(payload)[:32], "text": text,
                     "sha256": digest(text), "received_at": self.clock()}
            old = state["draft"]
            if state.get("reply_contract_version", 1) == 2 and old and old["proposal"]["kind"] == "budget_authorization_proposal":
                state.setdefault("draft_history", []).append({"reply_id": old["reply_id"],
                    **{key: old["proposal"][key] for key in ("amount", "currency", "order_id")},
                    "draft_sha256": old["draft_sha256"], "invalidated_by_reply_id": reply["reply_id"],
                    "invalidated_at": self.clock()})
            state["replies"].append(reply)
            state["draft"] = None
            state["reply_outcome"] = None
            return self._commit(state, operation_id, payload, {"reply_id": reply["reply_id"]})

    def record_reply_fallback(self, case_id, principal, *, reply_id,
                              reason="no_accepted_model_proposal"):
        """A failed interpretation is not converted into an accepted candidate."""
        require(reason == "no_accepted_model_proposal", "invalid_reply_fallback")
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._legacy_reply(state)
            self._receipt_current(state)
            require(state["replies"] and state["replies"][-1]["reply_id"] == reply_id, "reply_superseded")
            if state.get("closure_intent"):
                return self.public(state)
            require(state["status"] == "awaiting_human" and state.get("reply_contract_version", 1) == 2,
                    "case_not_waiting")
            outcome = {"accepted": False, "reply_id": reply_id, "reason": reason,
                "host_question": f"What budget cap do you authorize for {state['ticket']['order_name']} ({state['currency']['currency']})?"}
            if state.get("reply_outcome") != outcome or state["draft"] is not None:
                state["reply_outcome"], state["draft"] = outcome, None
                state["revision"] += 1
                self._write(state)
            return self.public(state)

    @staticmethod
    def _draft_context(state):
        ticket = state["ticket"]
        context = {"kind": "forgeflow.budget_confirmation_context.v1",
            "case_id": state["case_id"], "owner": state["owner"],
            "generation": state["generation"], "input_sha256": state["input_sha256"],
            "ticket_id": ticket["ticket_id"], "parent_episode": ticket["parent_episode"],
            "parent_policy_sha256": ticket["parent_policy_sha256"],
            "facts_sha256": ticket["facts_sha256"]}
        if state.get("decision_input_mode") == "structured_budget_v1":
            context.update(decision_input_mode=state["decision_input_mode"],
                           decision_contract_version=state["decision_contract_version"])
        return digest(context)

    def propose(self, case_id, principal, *, reply_id, proposal):
        """Host parser callback. This saves a draft, never grants authorization."""
        allowed = {"kind", "order_id", "amount", "currency", "question"}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._legacy_reply(state)
            self._receipt_current(state)
            if state.get("closure_intent"):
                require(any(row["reply_id"] == reply_id for row in state["replies"]), "reply_superseded")
                return self.public(state)
            require(state["status"] == "awaiting_human" and state["replies"]
                    and state["replies"][-1]["reply_id"] == reply_id, "reply_superseded")
            version = state.get("reply_contract_version", 1)
            require(isinstance(proposal, dict), "invalid_reply_proposal", 400)
            if version == 1:
                require(set(proposal) <= allowed and proposal.get("kind") in
                        {"budget_authorization_proposal", "price_fact", "ambiguous", "out_of_scope", "cancel", "pause"},
                        "invalid_reply_proposal", 400)
            value = copy.deepcopy(proposal)
            if value["kind"] == "budget_authorization_proposal":
                require(value.get("order_id") == state["ticket"]["order_id"], "wrong_budget_order")
                require(value.get("currency") == state["currency"]["currency"], "wrong_budget_currency")
                value["amount"] = amount_text(value.get("amount"))
            else:
                require(not {"amount", "order_id", "currency"} & set(value), "invalid_reply_proposal", 400)
            require(isinstance(value.get("question", ""), str) and len(value.get("question", "")) <= 1200,
                    "invalid_reply_proposal", 400)
            from forgeflow.investigation.clarification import ClarificationError, validate_proposal

            try:
                if version == 2:
                    from forgeflow.investigation.clarification_v2 import (
                        validate_proposal as validate_v2,
                    )
                    value = validate_v2(state["ticket"], state["replies"][-1]["text"], value,
                        state["currency"]["currency"], visible_replies=state["replies"], current_reply_id=reply_id)
                else:
                    value = validate_proposal(state["ticket"], state["replies"][-1]["text"],
                                              value, state["currency"]["currency"])
            except ClarificationError as error:
                raise CaseError(error.code, 400) from None
            draft = {"reply_id": reply_id, "reply_sha256": state["replies"][-1]["sha256"],
                     "proposal": value, "parent_policy_sha256": state["ticket"]["parent_policy_sha256"],
                     "context_sha256": self._draft_context(state)}
            if version == 2:
                from forgeflow.investigation.clarification_v2 import render_question
                draft["host_question"] = render_question(state["ticket"], state["currency"]["currency"], value)
                draft["host_reason"] = value.get("need", value["kind"])
            draft["draft_sha256"] = digest(draft)
            if state["draft"] != draft:
                state["draft"] = draft
                state["reply_outcome"] = None
                if value["kind"] == "cancel":
                    if version == 2:
                        self._start_closure(state, operation_id="reply-close-" + reply_id,
                                            reason="user_cancelled", expected_revision=state["revision"])
                    else:
                        state["status"] = "stopped"
                        state["terminal"] = {"status": "cancelled", "reason": "user_cancelled", "reply_id": reply_id}
                state["revision"] += 1
                self._write(state)
            return self.public(state)

    def confirm(self, case_id, principal, *, operation_id, expected_revision,
                draft_sha256, parent_policy_sha256, confirmed):
        require(confirmed is True, "explicit_confirmation_required", 400)
        payload = {"kind": "confirm", "revision": expected_revision, "draft_sha256": draft_sha256,
                   "parent_policy_sha256": parent_policy_sha256, "confirmed": True}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            prior = self._operation(state, operation_id, payload, expected_revision)
            if prior:
                self._materialize(state)
                return prior
            self._current(state)
            require(state["status"] == "awaiting_human" and state["confirmation"] is None,
                    "case_not_waiting")
            self._not_paused(state)
            draft = state["draft"]
            require(draft is not None and draft["draft_sha256"] == draft_sha256
                    and draft["parent_policy_sha256"] == parent_policy_sha256
                    and draft.get("context_sha256") == self._draft_context(state)
                    and draft["proposal"]["kind"] == "budget_authorization_proposal", "draft_not_confirmable")
            structured = state.get("decision_input_mode") == "structured_budget_v1"
            if structured:
                self._structured_waiting(state)
                input_row = self._structured_candidate(state, draft)
            policy, proof = self._decision_context_current(state)
            receipt = {"actor": principal, "operation_id": operation_id,
                       "draft_sha256": draft_sha256, "parent_policy_sha256": parent_policy_sha256,
                       "order_id": proof["order_id"], "before": None, "after": draft["proposal"]["amount"],
                       "currency": state["currency"]["currency"], "confirmed_at": self.clock()}
            if structured:
                receipt.update(source="structured_budget", input_id=draft["input_id"],
                               input_sha256=draft["input_sha256"], decision_input_mode=state["decision_input_mode"],
                               decision_contract_version=state["decision_contract_version"])
            contract = compile_policy(state["template"], policy, order_id=proof["order_id"],
                amount=float(receipt["after"]), revision_id=case_id + "-policy-2",
                receipt_hash=digest(receipt), now=self.clock())
            child_id = case_id + "-e2"
            parent_id = state["current_episode"]
            require(len(state["episodes"]) == 1, "child_episode_limit")
            state["confirmation"] = receipt
            if structured:
                input_row.update(status="confirmed", confirmed_at=receipt["confirmed_at"],
                                 confirmed_by_operation_id=operation_id)
            state["ticket"]["status"] = "confirmed"
            state["episodes"][child_id] = {"episode_id": child_id, "parent_episode": parent_id,
                "policy": contract.payload(), "policy_sha256": contract.digest(), "status": "pending"}
            state["continuation"] = {"operation_id": operation_id, "parent_episode": parent_id,
                "child_episode": child_id, "policy_sha256": contract.digest(), "phase": "pending"}
            state["current_episode"] = child_id
            state["status"] = "continuation_pending"
            result = self._commit(state, operation_id, payload,
                                  {"child_episode": child_id, "policy_sha256": contract.digest()})
            self._materialize(state)
            return result

    def reserve_work(self, case_id, principal, *, operation_id, kind, allowance):
        """Host worker reserves before dispatch; unknown usage cannot be refunded."""
        identifier(operation_id)
        require(kind in {"parent", "reply", "child", "product_prepare"}, "invalid_work_kind", 400)
        require(isinstance(allowance, dict) and set(allowance) == set(RESOURCE_FIELDS)
                and all(type(v) is int and v >= 0 for v in allowance.values()), "invalid_allowance", 400)
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._current(state)
            self._not_paused(state)
            if kind == "reply":
                self._legacy_reply(state)
            if operation_id in state["work"]:
                row = state["work"][operation_id]
                require(row["kind"] == kind and row["allowance"] == allowance, "operation_input_conflict")
                return copy.deepcopy(row)
            require(not any(row["phase"] == "reserved" for row in state["work"].values()), "worker_still_active")
            phases = {"parent": {"investigating"}, "reply": {"awaiting_human"},
                      "child": {"continuation_pending"}, "product_prepare": {"ready_for_review", "handoff_pending"}}
            require(state["status"] in phases[kind], "work_phase_mismatch")
            episode = state["episodes"][state["current_episode"]]
            if kind in {"parent", "child"}:
                require(not episode.get("work_operation_id"), "episode_already_dispatched")
            spent = {key: sum(row.get("usage", {}).get(key, 0) or 0 for row in state["work"].values())
                     for key in RESOURCE_FIELDS}
            require(all(spent[key] + allowance[key] <= state["limits"][key] for key in RESOURCE_FIELDS),
                    "case_budget_exhausted")
            row = {"kind": kind, "allowance": dict(allowance), "phase": "reserved", "usage": {},
                   "started_at": self.clock(), "episode_id": state["current_episode"]}
            state["work"][operation_id] = row
            if kind in {"parent", "child"}:
                episode["work_operation_id"] = operation_id
            state["revision"] += 1
            self._write(state)
            return copy.deepcopy(row)

    def finish_work(self, case_id, principal, *, operation_id, usage):
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            require(operation_id in state["work"], "work_not_found", 404)
            row = state["work"][operation_id]
            require(isinstance(usage, dict) and set(usage) == set(RESOURCE_FIELDS)
                    and all(v is None or type(v) in (int, float) and math.isfinite(v) and v >= 0
                            for v in usage.values()), "invalid_usage", 400)
            if row["phase"] != "reserved":
                require(row["usage"] == usage, "usage_receipt_conflict")
                return copy.deepcopy(row)
            row.update(usage=copy.deepcopy(usage), phase="unknown" if None in usage.values() else "complete",
                       finished_at=self.clock())
            # A reported in-flight overshoot is retained, not clamped to the
            # grant. The next reservation fails against actual cumulative use.
            if row["phase"] == "unknown" and not state.get("closure_intent"):
                state["status"] = "stopped"
                state["terminal"] = {"status": "technical_failure", "reason": "case_usage_unknown"}
            state["revision"] += 1
            self._write(state)
            return copy.deepcopy(row)

    def reject_runtime(self, case_id, principal, *, operation_id):
        """Host execution validation is independent of known provider usage."""
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            require(operation_id in state["work"] and state["work"][operation_id]["phase"] != "reserved",
                    "worker_still_active")
            if state.get("runtime_failure"):
                require(state["runtime_failure"]["operation_id"] == operation_id, "runtime_failure_conflict")
                return self.public(state)
            state["runtime_failure"] = {"operation_id": operation_id, "reason": "runtime_validation_failed"}
            if not state.get("closure_intent"):
                state["status"] = "stopped"
                state["terminal"] = {"status": "technical_failure", **state["runtime_failure"]}
            state["revision"] += 1
            self._write(state)
            return self.public(state)

    def handoff(self, case_id, principal, *, operation_id, expected_revision):
        payload = {"kind": "handoff", "revision": expected_revision}
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            prior = self._operation(state, operation_id, payload, expected_revision)
            if prior:
                return prior
            self._current(state)
            require(state["status"] == "ready_for_review" and state["handoff"] is None, "handoff_not_ready")
            require(self._currency(state["template"]) == state["currency"], "currency_binding_changed")
            episode = state["episodes"][state["current_episode"]]
            directory = self._directory(case_id) / "episodes" / episode["episode_id"] / "store"
            review = read_json(directory / "investigation" / "review.json")
            terminal = read_json(directory / "investigation" / "state.json")
            # A fresh product prepare does not excuse corrupt source evidence.
            # Validate all seven original artifacts and the prepared receipt.
            trusted_summary(terminal, review, store=directory)
            contract = contract_from_payload(episode["policy"])
            require(digest(terminal) == episode["terminal_sha256"] and terminal["status"] == "prepared"
                    and terminal["prepared"]["review_sha256"] == digest(review)
                    and review["run_id"] == episode["episode_id"]
                    and review["event_sha256"] == state["event"]["sha256"]
                    and review["contract_sha256"] == contract.digest()
                    and review["policy_sha256"] == contract.digest(), "handoff_binding_changed")
            request = review["request"]
            require(check_request(request, contract)["ok"], "handoff_contract_mismatch")
            plan_id = review["plan_id"]
            require(isinstance(plan_id, str) and re.fullmatch(r"[a-f0-9]{64}", plan_id), "invalid_plan_id")
            manifest = read_json(directory / plan_id / "manifest.json")
            require(digest(manifest) == plan_id and manifest["artifacts"]["request"] == digest(request)
                    and read_json(directory / plan_id / "request.json") == request, "handoff_artifact_changed")
            product_id = digest({"case": case_id, "operation": operation_id})[:32]
            binding = {"owner": principal, "generation": state["generation"],
                       "source_case": case_id, "source_episode": episode["episode_id"],
                       "source_review_sha256": digest(review), "event": state["event"],
                       "request": request, "contract": contract.payload(), "currency": state["currency"],
                       "product_case_id": product_id}
            state["handoff"] = {"operation_id": operation_id, "phase": "pending",
                                "dispatch_protocol": 1,
                                "product_case_id": product_id, "binding_sha256": digest(binding)}
            state["handoff_binding"] = binding
            state["status"] = "handoff_pending"
            return self._commit(state, operation_id, payload, copy.deepcopy(state["handoff"]))

    def handoff_context(self, case_id, principal, *, dispatch=False):
        """Trusted product adapter only; never a model-facing case endpoint."""
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._current(state)
            require(state["handoff"] is not None, "handoff_not_ready")
            binding = state["handoff_binding"]
            require(digest(binding) == state["handoff"]["binding_sha256"], "handoff_binding_changed")
            if dispatch and not state["handoff"].get("dispatch_started_at"):
                state["handoff"]["dispatch_started_at"] = self.clock()
                state["revision"] += 1
                self._write(state)
            return {"binding": copy.deepcopy(binding), "intent": copy.deepcopy(state["handoff"])}

    def complete_handoff(self, case_id, principal, *, product_case_id, binding_sha256, product_receipt):
        """Record a trusted create-from-handoff receipt, not an execution approval."""
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            self._receipt_current(state)
            intent = state["handoff"]
            require(intent is not None and intent["product_case_id"] == product_case_id
                    and intent["binding_sha256"] == binding_sha256
                    and product_receipt.get("case_id") == product_case_id, "handoff_binding_changed")
            if intent["phase"] == "complete":
                require(intent["product_receipt_sha256"] == digest(product_receipt), "handoff_receipt_conflict")
            else:
                intent.update(phase="complete", product_receipt_sha256=digest(product_receipt))
                if not state.get("closure_intent"):
                    state["status"] = "product_review"
                state["revision"] += 1
                self._write(state)
            return copy.deepcopy(intent)

    def release_environment(self, case_id, principal, *, product_closure=None):
        """Host-only closure after a known stop or verified product receipt.

        Unknown usage or execution never relinquishes this environment claim.
        Product closure is issued by the product service after checking its
        persisted case, readback/retirement proof and shared environment guard.
        """
        with scope_lock(self.root):
            state = self._read(case_id, principal)
            require(self.generation() == state["generation"], "environment_generation_changed")
            require(all(row["phase"] == "complete" for row in state["work"].values()), "case_usage_unknown")
            closure = state.get("closure_intent")
            unstarted_handoff = (closure and state["handoff"] is not None
                and state["handoff"].get("dispatch_protocol") == 1
                and "dispatch_started_at" not in state["handoff"])
            if state["handoff"] is None or unstarted_handoff:
                require((state["status"] == "stopped" or closure is not None)
                        and product_closure is None, "case_not_closed")
                receipt = {"kind": "handoff_not_dispatched" if unstarted_handoff else "investigation_stopped",
                           "terminal": state["terminal"]}
                if closure:
                    receipt["closure_reason"] = closure["reason"]
            else:
                require(isinstance(product_closure, dict)
                        and product_closure.get("schema_version") == 1
                        and product_closure.get("case_id") == state["handoff"]["product_case_id"]
                        and product_closure.get("binding_sha256") == state["handoff"]["binding_sha256"]
                        and product_closure.get("generation") == state["generation"]
                        and product_closure.get("environment_clear") is True
                        and product_closure.get("state") in {"succeeded", "failed", "rejected", "expired"}
                        and product_closure.get("outcome") in {"execution_validated", "not_started", "never_approved"},
                        "product_closure_unproven")
                receipt = copy.deepcopy(product_closure)
            if state.get("environment_released"):
                require(state["environment_released"]["receipt_sha256"] == digest(receipt), "closure_receipt_changed")
            path = self.root / "environment.json"
            environment = read_json(path)
            if state.get("environment_released") and environment["active_case"] != case_id:
                return copy.deepcopy(state["environment_released"])
            if closure and not state.get("environment_released"):
                require(environment["generation"] == state["generation"], "environment_generation_changed")
                saved = state.get("closure_receipt")
                if saved is not None:
                    require(saved == receipt and closure.get("receipt_sha256") == digest(receipt),
                            "closure_receipt_changed")
                else:
                    require(environment["active_case"] in {case_id, None}
                            and environment["write_operation"] is None, "environment_execution_unresolved")
                    state["closure_receipt"] = copy.deepcopy(receipt)
                    closure.update(phase="releasing", blockers=[], receipt=copy.deepcopy(receipt),
                                   receipt_sha256=digest(receipt))
                    state["revision"] += 1
                    self._write(state)
                # Record proof before clearing the claim, but report released
                # only after that write succeeds. A newer case can claim the
                # empty environment after a crash; never clear its occupation.
                require(self.generation() == state["generation"], "environment_generation_changed")
                environment = read_json(path)
                require(environment["generation"] == state["generation"], "environment_generation_changed")
                if environment["active_case"] in {case_id, None}:
                    require(environment["write_operation"] is None, "environment_execution_unresolved")
                    if environment["active_case"] == case_id:
                        environment["active_case"] = None
                        atomic_json(path, environment, replace=True)
                else:
                    require(saved is not None, "environment_execution_unresolved")
                state["environment_released"] = {"receipt_sha256": digest(receipt), "at": self.clock()}
                closure.update(phase="closed", blockers=[])
                state["status"] = "closed"
                state["revision"] += 1
                self._write(state)
                return copy.deepcopy(state["environment_released"])
            require(environment["generation"] == state["generation"]
                    and environment["active_case"] in {case_id, None}
                    and environment["write_operation"] is None, "environment_execution_unresolved")
            # Aggregate first; a crash before clearing the claim remains safe
            # and the same receipt completes release on retry.
            if not state.get("environment_released"):
                state["environment_released"] = {"receipt_sha256": digest(receipt), "at": self.clock()}
                state["closure_receipt"] = receipt
                if closure:
                    closure.update(phase="closed", blockers=[], receipt=copy.deepcopy(receipt))
                    state["status"] = "closed"
                state["revision"] += 1
                self._write(state)
            environment["active_case"] = None
            atomic_json(path, environment, replace=True)
            return copy.deepcopy(state["environment_released"])

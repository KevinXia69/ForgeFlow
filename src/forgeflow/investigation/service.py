"""Trusted investigation state and admission to the existing recovery planner.

Store this beside the executor recovery store with executor-only permissions.
One store serves one episode. Changing run_id cannot reset its resource budget.
"""
from __future__ import annotations

import copy
import math
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path

from forgeflow.contract import TaskContract
from forgeflow.investigation.event import trusted_event
from forgeflow.investigation.evidence import record_value, validate_draft
from forgeflow.investigation.policy import PartialPolicy
from forgeflow.investigation.sources import FIELDS, SourceError
from forgeflow.investigation.transport import response_fits
from forgeflow.planning.live import LiveInputError
from forgeflow.planning.tool import SolverFailure
from forgeflow.recovery import service as recovery
from forgeflow.recovery.change_set import ChangeSetError
from forgeflow.recovery.storage import atomic_json, digest, read_json, scope_lock

TERMINAL = frozenset({"prepared", "needs_clarification", "unsupported", "infeasible",
                      "budget_exhausted", "technical_failure"})
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")


class InvestigationError(ValueError):
    pass


def field_changes(previous, current, prefix=""):
    """Stable leaf changes; retain explicit absence separately from JSON null."""
    if isinstance(previous, dict) and isinstance(current, dict):
        changes = []
        for key in sorted(set(previous) | set(current)):
            path = f"{prefix}.{key}" if prefix else key
            if key in previous and key in current:
                changes.extend(field_changes(previous[key], current[key], path))
            elif key in previous:
                changes.append({"field": path, "before": previous[key], "after_missing": True})
            else:
                changes.append({"field": path, "before_missing": True, "after": current[key]})
        return changes
    if previous == current and type(previous) is type(current):
        return []
    return [{"field": prefix, "before": previous, "after": current}]


def invalid_draft_signature(draft, observations, validation, *, event=None):
    """Identify failed business input independently of citation/prose presentation.

    Only referenced records and validation's relevant live facts participate.
    Querying an unrelated record or refreshing an observation cannot reset it.
    Historical signatures live with the stored drafts, not in process memory.
    """
    def canonical(value):
        if isinstance(value, dict):
            return {key: canonical(item) for key, item in value.items()}
        if isinstance(value, list):
            return [canonical(item) for item in value]
        if isinstance(value, float):
            if not math.isfinite(value):
                return {"invalid_number": str(value)}
            return int(value) if value.is_integer() else value
        return value

    def unique(values):
        indexed = {digest(canonical(value)): canonical(value) for value in values}
        return [indexed[key] for key in sorted(indexed)]

    def record(value):
        if not isinstance(value, dict):
            return None
        entity = value.get("entity")
        fields = value.get("fields", {})
        allowed = FIELDS.get(entity, ())
        if entity == "sales_order":
            allowed = ("state", "commitment_date")
        elif entity == "policy":
            allowed = ("task_type", "anchor_date", "margin", "margin_basis", "time_granularity")
        elif entity == "order_policy":
            allowed = ("authorized", "budget_cap")
        name = fields.get("name")
        event_text = event.get("text", "") if isinstance(event, dict) else ""
        name_is_anchor = isinstance(name, str) and bool(name) and re.search(
            r"(?<![\w-])" + re.escape(name) + r"(?![\w-])", event_text, re.I)
        if not name_is_anchor:
            allowed = tuple(key for key in allowed if key != "name")
        return {"entity": entity, "id": value.get("id"),
                "fields": {key: fields[key] for key in allowed if key in fields}}

    draft = draft if isinstance(draft, dict) else {}
    request = draft.get("request")
    ids = request.get("sales_order_ids") if isinstance(request, dict) else None

    def field_path(value):
        match = re.fullmatch(r"sales_order_ids\.(\d+)", value) if isinstance(value, str) else None
        if match and isinstance(ids, list) and int(match[1]) < len(ids):
            return "sales_order_ids#" + digest(canonical(ids[int(match[1])]))
        return value

    def observed(identity):
        value = observations.get(identity) if isinstance(identity, str) else None
        return record(value.get("record")) if isinstance(value, dict) else None

    def reference_field(value):
        supported = {"target_product_code", "anchor_date", "margin", "disruption.kind", "disruption.vendor_ref"}
        if isinstance(request, dict) and isinstance(request.get("disruption"), dict):
            if "offer_key" in request["disruption"]:
                supported.add("disruption.offer_key")
        if isinstance(ids, list):
            supported.update(f"sales_order_ids.{index}" for index in range(len(ids)))
            supported.update(f"sales_order_budget_caps.{identity}" for identity in ids)
        return field_path(value) if isinstance(value, str) and value in supported else None

    semantic_request = request
    if isinstance(request, dict):
        keys = {"target_product_code", "sales_order_ids", "anchor_date", "margin",
                "sales_order_budget_caps", "disruption"}
        semantic_request = {key: request[key] for key in keys if key in request}
        semantic_request["extra_fields_present"] = bool(set(request) - keys)
        if isinstance(ids, list):
            semantic_request["sales_order_ids"] = unique(ids)
            semantic_request["duplicate_order_ids"] = len(unique(ids)) != len(ids)
        budgets = request.get("sales_order_budget_caps")
        if isinstance(budgets, dict):
            budget_keys = {str(identity) for identity in ids} if isinstance(ids, list) else set()
            semantic_request["sales_order_budget_caps"] = {key: value for key, value in budgets.items() if key in budget_keys}
            semantic_request["extra_budget_fields_present"] = bool(set(budgets) - budget_keys)
        disruption = request.get("disruption")
        if isinstance(disruption, dict):
            keys = {"kind", "vendor_ref", "offer_key", "workcenter_code"}
            semantic_request["disruption"] = {key: disruption[key] for key in keys if key in disruption}
            semantic_request["disruption"]["extra_fields_present"] = bool(set(disruption) - keys)
    references = draft.get("evidence")
    evidence = []
    if isinstance(references, list):
        for ref in references:
            if not isinstance(ref, dict):
                evidence.append({"invalid_reference_type": type(ref).__name__})
                continue
            field = reference_field(ref.get("field"))
            if field is None:
                evidence.append({"unsupported_reference_field": True})
                continue
            material = observed(ref.get("observation_id"))
            source_field = ref.get("source_field")
            observation = observations.get(ref.get("observation_id")) if isinstance(ref.get("observation_id"), str) else None
            fields = observation.get("record", {}).get("fields", {}) if isinstance(observation, dict) else {}
            if source_field != "id" and (not isinstance(source_field, str) or source_field not in fields):
                source_field = {"unsupported_source_field": True}
            evidence.append({"field": field, "source_field": source_field,
                "record": material,
                "valid_shape": set(ref) == {"field", "source_field", "observation_id"}})
        evidence = unique(evidence)
    else:
        evidence = {"invalid_evidence_type": type(references).__name__}
    # Self-reported uncertainty is prose, not a newly observed failure. A model
    # can rename/add unresolved fields without changing any business input.
    # Removing the last such issue still permits a valid draft below.
    failures = unique([{"code": item.get("code"), "field": field_path(item.get("field"))}
                       for item in validation.get("issues", [])
                       if item.get("code") != "unresolved_field"])
    facts = unique([record(value) for value in validation.get("verification_facts", {}).values()])
    return digest(canonical({"request": semantic_request, "evidence": evidence,
                             "supply": observed(draft.get("supply_observation_id")),
                             "failures": failures, "facts": facts}))


@dataclass(frozen=True)
class Limits:
    queries: int = 120
    validations: int = 36
    prepares: int = 3
    seconds: int = 3600

    def __post_init__(self):
        if any(type(v) is not int or v <= 0 for v in asdict(self).values()):
            raise ValueError("invalid_investigation_limits")


class InvestigationService:
    def __init__(self, *, source, contract, store: Path, bundle: Path,
                 connection_factory, event=None, limits: Limits | None = None, clock=time.time):
        if not isinstance(contract, (TaskContract, PartialPolicy)):
            raise InvestigationError("policy_required")
        self.source, self.policy = source, contract
        self.contract = contract if isinstance(contract, TaskContract) else None
        self.event = trusted_event(event)
        self.store, self.bundle = Path(store), Path(bundle)
        self.root = self.store / "investigation"
        self.connection_factory = connection_factory
        self.limits, self.clock = limits or Limits(), clock

    def _save(self, state):
        atomic_json(self.root / "state.json", state, replace=True)

    def _state(self, run_id):
        path = self.root / "state.json"
        if path.exists():
            state = read_json(path)
            if (state["run_id"] != run_id or state["policy_sha256"] != self.policy.digest()
                    or state["event_sha256"] != self.event["sha256"]):
                raise InvestigationError("investigation_binding_changed")
            if state["limits"] != asdict(self.limits):
                raise InvestigationError("investigation_limits_changed")
            if state["status"] == "preparing":
                self._terminal(state, "technical_failure", "preparation_interrupted")
            return state
        state = {"schema_version": 1, "run_id": run_id,
                 "policy_sha256": self.policy.digest(),
                 "contract_sha256": self.contract.digest() if self.contract else None,
                 "event": self.event, "event_sha256": self.event["sha256"],
                 "started_at": self.clock(),
                 "limits": asdict(self.limits), "usage": {"queries": 0, "validations": 0, "prepares": 0},
                 "status": "investigating", "terminal": None, "prepared": None,
                 "observations": {}, "drafts": [], "attempts": [], "events": []}
        self._save(state)
        return state

    def _terminal(self, state, status, reason, **detail):
        state["status"] = status
        state["terminal"] = {"reason": reason, **detail}
        self._save(state)

    def _public(self, state):
        return {"run_id": state["run_id"], "status": state["status"],
                # Only the initial guard response may offer a model closing turn.
                # A durable status read/retry/restart must never mint another one.
                "closing_allowed": False,
                "terminal": state["terminal"], "usage": copy.deepcopy(state["usage"]),
                "limits": state["limits"], "prepared": state["prepared"],
                "draft_revision": len(state["drafts"]),
                "issues": state["drafts"][-1]["validation"]["issues"] if state["drafts"] else []}

    def _consume(self, state, kind):
        if state["usage"][kind] >= state["limits"][kind]:
            self._terminal(state, "budget_exhausted", f"{kind}_limit")
            return False
        state["usage"][kind] += 1
        self._save(state)
        return True

    def _expired(self, state, *, preceding_stop=None):
        elapsed = self.clock() - state["started_at"]
        if elapsed < 0 or elapsed >= self.limits.seconds:
            details = {"preceding_stop": preceding_stop} if preceding_stop is not None else {}
            self._terminal(state, "budget_exhausted", "time_limit", **details)
            return True
        return False

    def _can_close_with_clarification(self, state):
        terminal = state.get("terminal") or {}
        if (state["status"] != "technical_failure"
                or terminal.get("reason") != "repeated_invalid_draft_without_new_evidence"
                or terminal.get("stop_origin") != "invalid_draft_guard"
                or terminal.get("basis") == "agent_reported"):
            return False
        # A model may report the same reason string. Only a service-issued stop
        # bound to its durable invalid-input event permits this one-way close.
        return any(event.get("action") == "repeated_invalid_draft"
                   and event.get("input_sha256") == terminal.get("input_sha256")
                   and event.get("prior_draft_id") == terminal.get("prior_draft_id")
                   for event in state["events"])

    def dispatch(self, run_id: str, action: str, payload: dict) -> dict:
        if (not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id)
                or not isinstance(action, str)
                or action not in {"query", "validate", "prepare", "status", "finish"}
                or not isinstance(payload, dict)):
            raise InvestigationError("invalid_investigation_request")
        with scope_lock(self.root):
            state = self._state(run_id)
            if action == "status":
                if payload:
                    raise InvestigationError("invalid_investigation_request")
                return self._public(state)
            if state["status"] in TERMINAL:
                if (action == "finish" and payload.get("status") == "needs_clarification"
                        and self._can_close_with_clarification(state)):
                    preceding_stop = {"status": state["status"],
                        "terminal": copy.deepcopy(state["terminal"]),
                        "usage": copy.deepcopy(state["usage"]), "draft_revision": len(state["drafts"])}
                    if self._expired(state, preceding_stop=preceding_stop):
                        return self._public(state)
                    return self._finish(state, payload, preceding_stop=preceding_stop)
                return self._public(state)
            if self._expired(state):
                return self._public(state)
            if action == "query":
                return self._query(state, payload)
            if action == "validate":
                return self._validate(state, payload)
            if action == "prepare":
                return self._prepare(state, payload)
            return self._finish(state, payload)

    def _query(self, state, payload):
        if (not {"kind", "arguments"} <= set(payload) <= {"kind", "arguments", "operation_id"}
                or not isinstance(payload["arguments"], dict)):
            raise InvestigationError("invalid_investigation_request")
        operation = payload.get("operation_id")
        if operation is not None and (not isinstance(operation, str)
                or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", operation)):
            raise InvestigationError("invalid_investigation_request")
        request_hash = digest({"kind": payload["kind"], "arguments": payload["arguments"]})
        receipt_id = digest(operation) if operation is not None else None
        receipts = state.get("query_receipts", {})
        if receipt_id in receipts:
            receipt = receipts[receipt_id]
            if receipt["request_sha256"] != request_hash:
                raise SourceError("query_operation_conflict")
            try:
                response = read_json(self.root / ("query-page-" + receipt_id + ".json"), limit=65536)
                if digest(response) != receipt["response_sha256"] or not response_fits(response):
                    raise ValueError("invalid_receipt")
            except (OSError, ValueError, RuntimeError):
                raise SourceError("query_receipt_unavailable") from None
            return response
        if not self._consume(state, "queries"):
            return self._public(state)
        try:
            response = self.source.query(payload["kind"], payload["arguments"])
        except SourceError as error:
            state["events"].append({"action": "query_failed", "query": copy.deepcopy(payload),
                                    "reason": error.code})
            self._save(state)
            raise
        if self._expired(state):
            return self._public(state)
        projected, observations = [], {}
        for record in response["records"]:
            observation_id = digest({"run_id": state["run_id"], "record": record,
                                     "policy_sha256": state["policy_sha256"],
                                     "event_sha256": state["event_sha256"]})
            observations[observation_id] = {
                "record": copy.deepcopy(record), "observed_at": self.clock()}
            projected.append({**record, "observation_id": observation_id})
        if len(set(state["observations"]) | set(observations)) > 2000:
            self._terminal(state, "budget_exhausted", "observation_limit")
            return self._public(state)
        result = {**self._public(state), "records": projected, "complete": response["complete"],
                  "next_cursor": response.get("next_cursor")}
        if not response_fits(result):
            state["events"].append({"action": "query_failed", "query": copy.deepcopy(payload),
                                    "reason": "query_response_too_large"})
            self._save(state)
            raise SourceError("query_response_too_large")
        # Form a deliverable, recoverable page before admitting its observations.
        # A crash before the state commit leaves only an orphan receipt; it is
        # never treated as successfully observed. A committed receipt replays
        # byte-equivalent data without rereading ERP or charging another query.
        # This records page formation, not acknowledgement by a remote client.
        if receipt_id is not None:
            atomic_json(self.root / ("query-page-" + receipt_id + ".json"), result, replace=True)
            state.setdefault("query_receipts", {})[receipt_id] = {
                "request_sha256": request_hash, "response_sha256": digest(result)}
        state["observations"].update(observations)
        state["events"].append({"action": "query", "query": copy.deepcopy(payload),
                                "page_formed": True,
                                "observations": [r["observation_id"] for r in projected]})
        self._save(state)
        return result

    def _validate(self, state, payload):
        if set(payload) != {"draft"}:
            raise InvestigationError("invalid_investigation_request")
        if not self._consume(state, "validations"):
            return self._public(state)
        draft = payload["draft"]
        validation = validate_draft(draft, state["observations"], self.source, self.contract,
                                    event=self.event)
        if self._expired(state):
            return self._public(state)
        failed_input = None
        if not validation["valid"]:
            failed_input = invalid_draft_signature(draft, state["observations"], validation, event=self.event)
            for prior in state["drafts"]:
                if prior["validation"]["valid"]:
                    continue
                previous_input = prior.get("failure_input_sha256") or invalid_draft_signature(
                    prior["draft"], state["observations"], prior["validation"], event=self.event)
                if previous_input == failed_input:
                    state["events"].append({"action": "repeated_invalid_draft",
                        "input_sha256": failed_input, "prior_draft_id": prior["draft_id"]})
                    state["closing_opportunity"] = {"offered_at": self.clock(),
                        "input_sha256": failed_input, "prior_draft_id": prior["draft_id"],
                        "max_provider_requests": 1, "consumed": True}
                    self._terminal(state, "technical_failure", "repeated_invalid_draft_without_new_evidence",
                                   stop_origin="invalid_draft_guard", input_sha256=failed_input,
                                   prior_draft_id=prior["draft_id"])
                    return {**self._public(state), "valid": False, "draft_id": None,
                            "closing_allowed": True}
        revision = len(state["drafts"]) + 1
        frozen = {"revision": revision, "draft": copy.deepcopy(draft),
                  "validation": validation, "contract_sha256": state["contract_sha256"],
                  "policy_sha256": state["policy_sha256"], "event_sha256": state["event_sha256"]}
        identity = digest(frozen)
        previous_draft = state["drafts"][-1]["draft"] if state["drafts"] else {}
        previous = previous_draft.get("request", {}) if isinstance(previous_draft, dict) else {}
        current = draft.get("request", {}) if isinstance(draft, dict) else {}
        previous = previous if isinstance(previous, dict) else {}
        current = current if isinstance(current, dict) else {}
        frozen["changes"] = field_changes(previous, current)
        frozen["changed_fields"] = [change["field"] for change in frozen["changes"]]
        frozen["draft_id"] = identity
        if failed_input is not None:
            frozen["failure_input_sha256"] = failed_input
        state["drafts"].append(frozen)
        self._save(state)
        return {**self._public(state), "valid": validation["valid"],
                "draft_id": identity if validation["valid"] else None,
                "request_sha256": validation.get("request_sha256"),
                "changed_fields": frozen["changed_fields"]}

    def _prepare(self, state, payload):
        if set(payload) != {"draft_id"} or not isinstance(payload["draft_id"], str):
            raise InvestigationError("invalid_investigation_request")
        if not state["drafts"]:
            raise InvestigationError("validated_draft_required")
        frozen = state["drafts"][-1]
        if frozen["draft_id"] != payload["draft_id"] or not frozen["validation"]["valid"]:
            raise InvestigationError("validated_draft_required")
        fresh = validate_draft(frozen["draft"], state["observations"], self.source, self.contract,
                               event=self.event)
        if self._expired(state):
            return self._public(state)
        if not fresh["valid"]:
            frozen["validation"] = fresh
            self._save(state)
            return {**self._public(state), "valid": False}
        request = fresh["request"]
        evidence_values = []
        for ref in sorted(frozen["draft"]["evidence"], key=lambda ref: ref["field"]):
            record = state["observations"][ref["observation_id"]]["record"]
            critical_fields = {"sales_order": ("state", "commitment_date"),
                               "supplier_offer": ("supplier_id", "product_id", "product_template_id",
                                                  "offer_key", "unit_price", "lead_days", "minimum_quantity")}
            evidence_values.append({"field": ref["field"], "entity": record["entity"],
                                    "id": record["id"], "source_field": ref["source_field"],
                                    "value": record_value(record, ref["source_field"]),
                                    "critical": {key: record["fields"].get(key)
                                                 for key in critical_fields.get(record["entity"], ())}})
        supply_record = state["observations"][frozen["draft"]["supply_observation_id"]]["record"]
        evidence_sha = digest({"request": request, "evidence": evidence_values,
                               "facts_sha256": fresh["facts_sha256"],
                               "supply": {"id": supply_record["id"], **{
                                   key: supply_record["fields"].get(key)
                                   for key in ("supplier_id", "state", "origin")}}})
        if any(prior["input_sha256"] == evidence_sha for prior in state["attempts"]):
            self._terminal(state, "technical_failure", "repeated_failure_without_new_evidence")
            return self._public(state)
        if not self._consume(state, "prepares"):
            return self._public(state)
        attempt = {"draft_id": frozen["draft_id"], "request_sha256": digest(request),
                   "input_sha256": evidence_sha, "result": "preparing"}
        state["attempts"].append(attempt)
        state["status"] = "preparing"
        self._save(state)
        try:
            summary = recovery.prepare(self.connection_factory(), request, store=self.store,
                                       bundle=self.bundle, contract=self.contract)
            if self._expired(state):
                attempt["result"] = "time_limit"
                self._save(state)
                return self._public(state)
            plan_id = summary["plan_id"]
            manifest = read_json(self.store / plan_id / "manifest.json")
            actual = read_json(self.store / plan_id / "request.json")
            if (digest(actual) != digest(request) or manifest["artifacts"]["request"] != digest(request)
                    or digest(manifest) != plan_id):
                raise InvestigationError("prepared_request_binding_changed")
            if "purchase_order_id" not in request["disruption"]:
                # Research clients can submit the earlier lane-only request format.
                # Their cited PO still scopes what may leave investigation for review.
                changes = read_json(self.store / plan_id / "change-set.json")
                if digest(changes) != manifest["artifacts"]["change-set"]:
                    raise InvestigationError("prepared_change_set_binding_changed")
                cancelled = [op["target"]["id"] for op in changes["operations"]
                             if op["kind"] == "purchase_order" and op["action"] == "cancel"]
                if cancelled != [int(supply_record["id"])]:
                    raise ChangeSetError("Cancellation exceeds cited purchase",
                                         code="additional_purchase_cancellation_unsupported")
            after = validate_draft(frozen["draft"], state["observations"], self.source, self.contract,
                                   event=self.event)
            if self._expired(state):
                attempt["result"] = "time_limit"
                self._save(state)
                return self._public(state)
            if not after["valid"] or after["facts_sha256"] != fresh["facts_sha256"]:
                frozen["validation"] = after
                attempt["result"] = "facts_changed_during_prepare"
                state["status"] = "investigating"
                self._save(state)
                return self._public(state)
            evidence = {ref["observation_id"]: state["observations"][ref["observation_id"]]
                        for ref in frozen["draft"]["evidence"]}
            sid = frozen["draft"]["supply_observation_id"]
            evidence[sid] = state["observations"][sid]
            review = {"schema_version": 1, "run_id": state["run_id"], "plan_id": plan_id,
                      "request": request, "request_sha256": digest(request),
                      "contract_sha256": self.contract.digest(), "draft_id": frozen["draft_id"],
                      "policy_sha256": self.policy.digest(), "event_sha256": self.event["sha256"],
                      "event": self.event,
                      "draft_revision": frozen["revision"], "evidence": evidence,
                      "facts_sha256": fresh["facts_sha256"],
                      "verification_facts": fresh["verification_facts"],
                      "field_evidence": frozen["draft"]["evidence"],
                      "summary": summary, "bindings_verified": True,
                      "odoo_writes": False, "execution_validated": False,
                      "benchmark_success": None}
            atomic_json(self.root / "review.json", review, replace=True)
            state["prepared"] = {"plan_id": plan_id, "request_sha256": digest(request),
                                 "review_sha256": digest(review), "bindings_verified": True}
            attempt["result"] = "prepared"
            self._terminal(state, "prepared", "validated_preparation")
        except SolverFailure as error:
            reason = str(error)
            attempt["result"] = reason
            proof = {"request_sha256": digest(request), "draft_id": frozen["draft_id"],
                     "facts_sha256": fresh["facts_sha256"],
                     "blueprint_sha256": getattr(error, "blueprint_sha256", None),
                     "blueprint": getattr(error, "blueprint", None), "trace": error.trace}
            attempt["solver_proof"] = proof
            attempt["solver_proof_sha256"] = digest(proof)
            status = "infeasible" if reason == "planning_model_infeasible" else "technical_failure"
            if not self._expired(state):
                self._terminal(state, status, reason if reason in {
                    "planning_model_infeasible", "solver_incomplete"} else "solver_failure")
        except LiveInputError as error:
            reason = error.code or "live_input_rejected"
            attempt["result"] = reason
            if reason == "unselected_active_demand":
                raw = (error.details or {}).get("missing_sales_order_ids", [])
                ids = list(dict.fromkeys(x for x in raw if type(x) is int and 0 < x <= 2**53 - 1))
                attempt["feedback"] = {"reason": reason, "missing_sales_order_ids": ids[:20],
                                       "complete": len(ids) <= 20}
            safe = {"unselected_active_demand", "broken_offer_ambiguous", "broken_offer_missing"}
            if reason == "shared_offer_cancellation_unsupported":
                self._terminal(state, "unsupported", reason)
            elif reason == "cancellation_subject_changed":
                self._terminal(state, "technical_failure", reason)
            elif reason in safe:
                state["status"] = "investigating"
                self._save(state)
            else:
                self._terminal(state, "technical_failure", "live_input_rejected")
        except ChangeSetError as error:
            attempt["result"] = error.code or "change_set_rejected"
            if error.code == "additional_purchase_cancellation_unsupported":
                self._terminal(state, "unsupported", error.code)
            elif error.code == "unsupported_recovery_scope":
                self._terminal(state, "unsupported", error.code,
                               supported_scope="one_manufactured_product_one_level_bom")
            else:
                self._terminal(state, "technical_failure", "change_set_rejected")
        except (recovery.RecoveryError, InvestigationError) as error:
            attempt["result"] = str(error)
            self._terminal(state, "technical_failure", "preparation_rejected")
        except Exception:
            attempt["result"] = "technical_failure"
            self._terminal(state, "technical_failure", "preparation_failed")
        if state["status"] != "prepared" and self._expired(state):
            return {**self._public(state), "feedback": {"reason": "time_limit"}}
        safe_reasons = {"prepared", "planning_model_infeasible", "solver_incomplete",
                        "unselected_active_demand", "broken_offer_ambiguous", "broken_offer_missing",
                        "facts_changed_during_prepare", "live_input_rejected", "technical_failure"}
        safe_reasons.update({"unsupported_recovery_scope", "additional_purchase_cancellation_unsupported", "shared_offer_cancellation_unsupported",
                             "cancellation_subject_changed"})
        feedback = attempt.get("feedback", {"reason": attempt["result"]
                                             if attempt["result"] in safe_reasons else "preparation_failed"})
        return {**self._public(state), "feedback": feedback}

    def _finish(self, state, payload, *, preceding_stop=None):
        if (set(payload) != {"status", "reason", "question"}
                or not isinstance(payload["status"], str)
                or payload["status"] not in {"needs_clarification", "unsupported", "technical_failure"}
                or not isinstance(payload["reason"], str) or not 1 <= len(payload["reason"]) <= 200
                or not isinstance(payload["question"], str) or len(payload["question"]) > 1200):
            raise InvestigationError("invalid_investigation_request")
        if payload["status"] == "needs_clarification" and not payload["question"].strip():
            raise InvestigationError("specific_question_required")
        details = {"preceding_stop": preceding_stop} if preceding_stop is not None else {}
        self._terminal(state, payload["status"], payload["reason"],
                       question=payload["question"], basis="agent_reported", **details)
        return self._public(state)

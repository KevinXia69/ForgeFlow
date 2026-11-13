"""Business-level Odoo 20 service: prepare, approve, execute and verify a plan.

The important departures from the previous single-cancellation slice:

* a plan carries a **complete action set** (preserve, cancel, create, adjust, link,
  reserve) and an approval is bound to that exact set;
* the execution identity is generated once, recorded on the approval, and
  re-checked by the addon on every action;
* read-back and acceptance are separate, independent reads.
"""
from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime
from typing import Any

from .adapter import Odoo20Adapter
from .contracts import ACTION_TYPES, CREATING_ACTIONS, canonical_review_action
from .executor import ExecutionResult, Odoo20BusinessExecutor, PlanExecutionResult
from .verifier import Odoo20BusinessVerifier, VerificationResult


class PlanPreparationError(ValueError):
    pass


PURCHASE_LINE_REVIEW_LIMIT = 500


def require_complete_review_snapshot(facts: dict[str, Any]) -> None:
    """Explicitly partial evidence may be inspected, but cannot authorize writes."""
    completeness = facts.get("completeness")
    if completeness is None:
        return  # Older snapshots retain their own validation and fact digest.
    if (not isinstance(completeness, dict)
            or completeness.get("complete") is False
            or completeness.get("truncated") is True
            or completeness.get("lines_complete") is False
            or completeness.get("query_failures")
            or completeness.get("missing_relations")):
        raise PlanPreparationError("recovery_plan_snapshot_incomplete")


def _environment_view(adapter) -> dict:
    """Environment identity as plain data (the dataclass uses slots)."""
    if not hasattr(adapter, "environment_generation"):
        return {}
    environment = adapter.environment_generation()
    return {"generation": environment.generation, "database": environment.database,
            "version": environment.version, "module_count": environment.module_count}


def _digest(value: Any) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                     default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _expiry_iso(value: Any) -> str:
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        raise PlanPreparationError("recovery_plan_expiry_invalid") from None
    return moment.replace(tzinfo=moment.tzinfo or UTC).isoformat()


@dataclass(frozen=True, slots=True)
class PreparedCancellation:
    """A prepared plan.  The name is historical; the payload is now a full plan."""

    exception_id: int
    plan_id: int
    plan_hash: str
    snapshot_hash: str
    facts: dict[str, Any]
    environment_generation: str = ""
    execution_identity: str = ""
    rule_version: int = 1
    actions: tuple[dict[str, Any], ...] = ()
    plan_state: str = "awaiting_approval"
    plan_version: int = 0
    plan_digest: str = ""
    action_digest: str = ""
    expires_at: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def action_ids(self) -> tuple[str, ...]:
        return tuple(str(action.get("action_id")) for action in self.actions)


@dataclass(frozen=True, slots=True)
class PreparedRecovery(PreparedCancellation):
    pass


class Odoo20BusinessService:
    """Deterministic metadata preparation plus the only approved apply gateway."""

    def __init__(self, adapter: Odoo20Adapter) -> None:
        self.adapter = adapter
        self.executor = Odoo20BusinessExecutor(adapter)
        self.verifier = Odoo20BusinessVerifier(adapter)

    # -- facts ---------------------------------------------------------------
    def _purchase_order_lines(self, order_id: int) -> tuple[list[dict], dict[str, Any]]:
        from .resolver import LINE_FIELDS

        domain = [("order_id", "=", int(order_id))]
        lines = self.adapter.search_read(
            "purchase.order.line", domain, LINE_FIELDS,
            limit=PURCHASE_LINE_REVIEW_LIMIT, order="id asc")
        # The RPC adapter caps a single request at 500. Probe one extra row
        # instead of asking for 501 and mistaking a capped page for a full list.
        more = (self.adapter.search_read(
            "purchase.order.line", domain, ["id"], limit=1,
            offset=PURCHASE_LINE_REVIEW_LIMIT, order="id asc")
            if len(lines) == PURCHASE_LINE_REVIEW_LIMIT else [])
        truncated = bool(more) or len(lines) > PURCHASE_LINE_REVIEW_LIMIT
        return lines[:PURCHASE_LINE_REVIEW_LIMIT], {
            "complete": not truncated, "lines_complete": not truncated,
            "truncated": truncated, "line_limit": PURCHASE_LINE_REVIEW_LIMIT,
            "reason": "purchase_order_line_limit_exceeded" if truncated else "",
        }

    def snapshot_purchase_order(self, order_id: int) -> dict[str, Any]:
        from .resolver import ORDER_FIELDS

        rows = self.adapter.search_read(
            "purchase.order", [("id", "=", int(order_id))], ORDER_FIELDS, limit=1)
        if not rows:
            raise PlanPreparationError("purchase_order_not_found")
        lines, completeness = self._purchase_order_lines(order_id)
        return {"order": rows[0], "lines": lines, "completeness": completeness,
                "captured_at": datetime.now(UTC).isoformat()}

    def recovery_snapshot(self, *, order_id: int,
                          company_ids: tuple[int, ...] | None = None
                          ) -> dict[str, Any]:
        """The fact snapshot an approval is bound to.

        Demand is derived from supply/demand evidence, never from a partner id:
        the previous version looked up sale lines whose *customer* id equalled the
        purchase order's *supplier* id, which is how a supplier became a customer.

        The snapshot also carries provenance (database, observation time),
        business versions, units, currency, stock location/reservation state and
        an explicit completeness block.  A truncated or failed relation read stays
        visible as unknown; it never becomes "nothing is affected".
        """
        from .resolver import BusinessObjectResolver

        resolver = BusinessObjectResolver(self.adapter, company_ids=company_ids)
        snapshot = resolver.snapshot(f"purchase.order:{int(order_id)}")
        record = snapshot.get("record") or {}
        order = record.get("record") if isinstance(record, dict) else None
        if not order:
            reason = (snapshot.get("completeness") or {}).get("reason")
            if reason == "ambiguous_reference":
                raise PlanPreparationError("purchase_order_ambiguous")
            if reason == "not_found":
                # Not visible in this tenant's company scope, or absent.  There
                # is no unscoped fallback read: an out-of-company order must not
                # become a plan.
                raise PlanPreparationError("purchase_order_not_found")
            facts = self.snapshot_purchase_order(order_id)
            order, lines = facts["order"], facts["lines"]
            line_completeness = facts["completeness"]
        else:
            lines, line_completeness = self._purchase_order_lines(order_id)
        completeness = dict(snapshot.get("completeness") or {})
        completeness.update({"lines_complete": line_completeness["lines_complete"],
                             "line_limit": PURCHASE_LINE_REVIEW_LIMIT})
        if line_completeness["truncated"]:
            completeness.update({"complete": False, "truncated": True,
                                 "reason": line_completeness["reason"]})
        relations = snapshot.get("relations") or {}
        return {
            "object_ref": snapshot.get("object_ref"),
            "order": order,
            "lines": lines,
            "captured_at": snapshot.get("observed_at"),
            "affected_targets": [f"purchase.order:{int(order_id)}"],
            # Direct demand is evidenced by a stock move that links the purchase
            # line to the sale line; potential/uncertain demand is labelled as
            # such and never promoted to direct.
            "sales_lines": relations.get("direct", []),
            "relations": relations,
            "completeness": completeness,
            "source": snapshot.get("source") or {},
            "environment": _environment_view(self.adapter),
        }

    # -- prepare -------------------------------------------------------------
    def prepare_recovery(self, exception_id: int, actions: list[dict],
                         facts: dict[str, Any], *, rule_version: int = 1,
                         tenant_ref: str = "", name: str = "Recovery plan",
                         expires_in_hours: int = 72) -> PreparedRecovery:
        for action in actions:
            if str(action.get("type")) not in ACTION_TYPES:
                raise PlanPreparationError(f"action_type_not_allowed:{action.get('type')}")
        environment = self.adapter.environment_generation()
        plan_id = self.adapter.prepare_plan(
            exception_id, actions, facts,
            environment_generation=environment.generation,
            rule_version=rule_version, tenant_ref=tenant_ref, name=name,
            expires_in_hours=expires_in_hours)
        # The addon canonicalizes and orders actions before persisting them.
        # Return that exact readback, rather than mixing its digests with the
        # caller's original action order or mutable fact dictionaries.
        prepared = self.load_prepared(plan_id)
        values = {item.name: getattr(prepared, item.name) for item in fields(prepared)}
        values["execution_identity"] = self.new_execution_identity(tenant_ref, plan_id)
        values["detail"] = {**prepared.detail, "environment": {
            "generation": environment.generation, "database": environment.database,
            "version": environment.version, "module_count": environment.module_count}}
        return PreparedRecovery(**values)

    def prepare_cancel(self, order_id: int, *, actor_uid: int,
                       reason: str,
                       company_ids: tuple[int, ...] | None = None) -> PreparedRecovery:
        # The approval is bound to the complete fact snapshot, not only the
        # order header: relations and completeness are part of what was approved.
        facts = self.recovery_snapshot(order_id=order_id, company_ids=company_ids)
        order = facts["order"]
        if order["state"] not in ("purchase", "draft", "sent", "to approve"):
            raise PlanPreparationError("purchase_order_not_cancellable")
        exception = self.adapter.create("forgeflow.exception_event", {
            "name": f"Supplier disruption {order['name']}",
            "event_type": "supplier_disruption",
            "partner_id": order["partner_id"][0] if order.get("partner_id") else False,
            "occurred_at": datetime.now(UTC).strftime("%Y-%m-%d %H:%M:%S"),
            "description": reason,
        })
        actions = [{
            "action_id": "cancel-po",
            "type": "cancel_purchase_order",
            "target_model": "purchase.order",
            "target_id": int(order_id),
            "params": {},
            "preconditions": [{"field": "state", "in": ["purchase", "sent", "draft",
                                                        "to approve"]}],
            "summary": f"Cancel {order['name']}",
        }]
        facts = {**facts, "affected_targets": [f"purchase.order:{int(order_id)}"]}
        return self.prepare_recovery(
            exception, actions, facts,
            name=f"Cancel {order['name']}",
            tenant_ref=str(order.get("company_id") or ""))

    @staticmethod
    def new_execution_identity(tenant_ref: str, plan_id: int) -> str:
        return f"{tenant_ref or 'tenant'}:{plan_id}:{uuid.uuid4().hex}"

    def load_prepared(self, plan_id: int) -> PreparedCancellation:
        row = self._plan_row(plan_id)
        try:
            payload = json.loads(row.get("payload_json") or "{}")
            if not isinstance(payload, dict):
                raise ValueError("payload_not_object")
            actions = payload.get("actions")
        except (TypeError, ValueError):
            raise PlanPreparationError("recovery_plan_payload_invalid") from None
        # Facts live in their own field, but their digest is part of the approval.
        # Never display an unverified snapshot beside an otherwise valid identity.
        try:
            facts = json.loads(row.get("fact_snapshot_json") or "{}")
        except (TypeError, ValueError):
            raise PlanPreparationError("recovery_plan_snapshot_invalid") from None
        if not isinstance(facts, dict):
            raise PlanPreparationError("recovery_plan_snapshot_invalid")
        if (not isinstance(actions, list) or not actions
                or any(not isinstance(action, dict) for action in actions)
                or len(actions) != row.get("action_count")):
            raise PlanPreparationError("recovery_plan_actions_incomplete")
        try:
            canonical_actions = [canonical_review_action(action) for action in actions]
            identifiers = [action["action_id"] for action in canonical_actions]
            version = int(row.get("version") or 0)
            rule_version = int(row.get("rule_version") or 1)
        except (TypeError, ValueError):
            raise PlanPreparationError("recovery_plan_payload_invalid") from None
        if (not all(identifiers) or len(set(identifiers)) != len(identifiers)
                or any(action["type"] not in ACTION_TYPES for action in canonical_actions)):
            raise PlanPreparationError("recovery_plan_actions_incomplete")
        if _digest(canonical_actions) != row.get("action_digest"):
            raise PlanPreparationError("recovery_plan_action_digest_mismatch")
        if _digest(facts) != row.get("fact_snapshot_hash"):
            raise PlanPreparationError("recovery_plan_snapshot_digest_mismatch")
        expected_digest = _digest({
            "tenant": row.get("tenant_ref") or "",
            "environment_generation": row.get("environment_generation") or "",
            "plan_id": int(row["id"]), "plan_version": version,
            "fact_snapshot_hash": row.get("fact_snapshot_hash") or "",
            "rule_version": rule_version, "actions": canonical_actions,
        })
        if version <= 0 or expected_digest != row.get("plan_digest"):
            raise PlanPreparationError("recovery_plan_digest_mismatch")
        exception_id = row.get("exception_id")
        if isinstance(exception_id, list) and exception_id:
            exception_id = int(exception_id[0])
        if not isinstance(exception_id, int):
            raise PlanPreparationError("recovery_plan_exception_missing")
        # The execution identity belongs to the approval, not to the loader: an
        # execution may only run as the identity the approver authorised.
        decisions = self.adapter.search_read(
            "forgeflow.decision_record",
            [("plan_id", "=", int(plan_id)), ("decision", "=", "approved"),
             ("revoked_at", "=", False)],
            ["id", "execution_identity", "action_digest", "plan_digest", "expires_at"],
            limit=1, order="id desc")
        approval = decisions[0] if decisions else {}
        if approval and (approval.get("action_digest") != row.get("action_digest")
                         or approval.get("plan_digest") != row.get("plan_digest")):
            raise PlanPreparationError("recovery_plan_approval_digest_mismatch")
        return PreparedCancellation(
            exception_id=exception_id, plan_id=int(row["id"]),
            plan_hash=str(row.get("plan_hash") or ""),
            snapshot_hash=str(row.get("fact_snapshot_hash") or ""),
            facts=facts, environment_generation=str(
                row.get("environment_generation") or ""),
            execution_identity=str(approval.get("execution_identity") or ""),
            rule_version=int(row.get("rule_version") or 1),
            actions=tuple(canonical_actions),
            plan_state=str(row.get("state") or ""),
            plan_version=int(row.get("version") or 0),
            plan_digest=str(row.get("plan_digest") or ""),
            action_digest=str(row.get("action_digest") or ""),
            expires_at=_expiry_iso(row.get("expires_at")),
            detail={"action_digest": row.get("action_digest"),
                    "action_count": row.get("action_count"),
                    "approval_expires_at": _expiry_iso(approval.get("expires_at"))})

    def _plan_row(self, plan_id: int) -> dict[str, Any]:
        rows = self.adapter.search_read(
            "forgeflow.recovery_plan", [("id", "=", int(plan_id))],
            ["id", "name", "state", "version", "plan_hash", "plan_digest",
             "action_digest", "action_count", "fact_snapshot_hash",
             "fact_snapshot_json", "environment_generation", "rule_version",
             "payload_json", "exception_id", "expires_at", "tenant_ref"],
            limit=1)
        if not rows:
            raise PlanPreparationError("recovery_plan_not_found")
        return rows[0]

    # -- approve -------------------------------------------------------------
    def approve(self, prepared: PreparedCancellation, *, approver_uid: int,
                comment: str, expires_in_hours: int = 24) -> int:
        """Approve exactly this plan version and action set."""
        require_complete_review_snapshot(prepared.facts)
        execution_identity = prepared.execution_identity or self.new_execution_identity(
            "", prepared.plan_id)
        decision = self.adapter.approve_plan(
            prepared.plan_id, int(approver_uid), execution_identity,
            expires_in_hours=expires_in_hours, comment=comment,
            expected_plan_version=prepared.plan_version,
            expected_plan_digest=prepared.plan_digest,
            expected_action_digest=prepared.action_digest)
        return decision

    def revoke(self, prepared: PreparedCancellation, *, reason: str = "") -> dict[str, Any]:
        """Withdraw an approval that has not been executed."""
        result = self.adapter.revoke_approval(prepared.plan_id, reason)
        self.adapter.write("forgeflow.exception_event", [prepared.exception_id],
                           {"state": "awaiting_approval"})
        return result

    # -- execute -------------------------------------------------------------
    def apply_plan(self, prepared: PreparedCancellation, *,
                   execution_identity: str | None = None) -> PlanExecutionResult:
        identity = execution_identity or prepared.execution_identity
        if not identity:
            raise PlanPreparationError("execution_identity_required")
        # A re-approval has a new execution identity; its operation keys must not
        # collide with the cancelled approval's keys.
        identity_suffix = identity.rsplit(":", 1)[-1][:12]
        prefix = f"{prepared.plan_id}:{prepared.plan_hash[:12]}:{identity_suffix}"
        self.adapter.write("forgeflow.exception_event", [prepared.exception_id],
                           {"state": "executing"})
        result = self.executor.apply_plan(prepared.plan_id, prefix, identity)
        self.adapter.write("forgeflow.exception_event", [prepared.exception_id],
                           {"state": "verifying" if result.succeeded else "needs_action"})
        return result

    def apply_cancel(self, prepared: PreparedCancellation, *,
                     idempotency_key: str) -> ExecutionResult:
        """Compatibility path for a single-action plan."""
        order_id = int(prepared.facts["order"]["id"])
        return self.executor.apply(
            prepared.plan_id, idempotency_key, "cancel_purchase_order",
            "purchase.order", order_id, {})

    def reconcile(self, prepared: PreparedCancellation, *,
                  idempotency_key: str) -> ExecutionResult:
        return self.executor.reconcile(prepared.plan_id, idempotency_key)

    def mark_result_unknown(self, prepared: PreparedCancellation, *,
                            idempotency_key: str, reason: str = "") -> dict:
        return self.executor.mark_result_unknown(prepared.plan_id, idempotency_key,
                                                 reason)

    def resolve_unknown(self, prepared: PreparedCancellation, *,
                        idempotency_key: str, confirmed_applied: bool) -> dict:
        return self.executor.resolve_unknown(prepared.plan_id, idempotency_key,
                                             confirmed_applied)

    # -- verify --------------------------------------------------------------
    def verify_plan(self, prepared: PreparedCancellation) -> VerificationResult:
        return self.verifier.verify_plan(prepared.plan_id)

    def verify(self, prepared: PreparedCancellation, *,
               idempotency_key: str) -> VerificationResult:
        return self.verifier.verify_purchase_cancel(prepared.plan_id, idempotency_key)

    def plan_checks(self, prepared: PreparedCancellation) -> VerificationResult:
        checks, detail = self.verifier.plan_checks(prepared.plan_id)
        status = "verified" if checks and all(c.status == "pass" for c in checks) else (
            "failed" if any(c.status == "failed" for c in checks) else "unknown")
        return VerificationResult(status, tuple(checks),
                                  {"plan": status, "detail": detail})

    @staticmethod
    def creates_document(action_type: str) -> bool:
        return str(action_type) in CREATING_ACTIONS

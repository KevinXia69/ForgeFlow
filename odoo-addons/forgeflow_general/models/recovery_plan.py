import json

from odoo import fields, models
from odoo.exceptions import AccessError, UserError

from . import recovery_logic
from .authorization import controlled, permission
from .recovery_logic import clean_for_rpc


class ForgeFlowRecoveryPlan(models.Model):
    _name = "forgeflow.recovery_plan"
    _inherit = "forgeflow.controlled"
    _description = "ForgeFlow recovery plan"
    _order = "id desc"

    name = fields.Char(required=True)
    exception_id = fields.Many2one("forgeflow.exception_event", required=True, index=True)
    company_id = fields.Many2one("res.company", required=True,
                                 default=lambda self: self.env.company)
    version = fields.Integer(default=1, required=True)
    plan_hash = fields.Char(required=True, index=True)
    plan_digest = fields.Char(index=True)
    action_digest = fields.Char(index=True)
    action_count = fields.Integer(default=0)
    fact_snapshot_hash = fields.Char(required=True)
    fact_snapshot_json = fields.Text()
    policy_version = fields.Integer(default=1, required=True)
    rule_version = fields.Integer(default=1, required=True)
    #: Digest of the confirmed rule set at preparation time.
    rule_epoch = fields.Char(index=True)
    #: The plan's own rule scope: company, target models and object references.
    #: A rule change outside this scope must not invalidate the plan.
    rule_scope_json = fields.Text()
    environment_generation = fields.Char(index=True)
    tenant_ref = fields.Char(index=True)
    #: Plan-level execution identity.  It is derived from the plan version, the
    #: approval's execution identity and the plan digest, so changing a caller's
    #: retry prefix still lands on the same attempt.
    execution_key = fields.Char(index=True)
    execution_attempts = fields.Integer(default=0)
    execution_error = fields.Char()
    executed_at = fields.Datetime()
    payload_json = fields.Text()
    state = fields.Selection(
        [("draft", "Draft"), ("awaiting_approval", "Awaiting approval"),
         ("approved", "Approved"), ("rejected", "Rejected"), ("stale", "Stale"),
         ("executing", "Executing"), ("verifying", "Verifying"),
         ("executed", "Executed"), ("partially_executed", "Partially executed"),
         ("execution_failed", "Execution failed"), ("expired", "Expired")],
        default="draft", required=True, index=True)
    expires_at = fields.Datetime()
    active = fields.Boolean(default=True)

    # ------------------------------------------------------------------ helpers
    def _forgeflow_lock(self):
        """Serialize approval, execution and revocation for one plan.

        PostgreSQL also rejects a stale repeatable-read snapshot when a locked
        row changed. Never reuse an ORM cache populated before obtaining it.
        """
        self.ensure_one()
        self.env.cr.execute("SELECT id FROM forgeflow_recovery_plan WHERE id=%s FOR UPDATE",
                            (self.id,))
        self.invalidate_recordset()

    def _forgeflow_payload(self):
        self.ensure_one()
        try:
            return json.loads(self.payload_json or "{}")
        except ValueError:
            raise UserError("plan_payload_invalid") from None

    def _forgeflow_actions(self):
        payload = self._forgeflow_payload()
        try:
            return recovery_logic.execution_order(payload.get("actions") or [])
        except recovery_logic.PlanError as error:
            raise UserError(error.code) from None

    def _forgeflow_digest(self):
        self.ensure_one()
        actions = self._forgeflow_actions()
        return recovery_logic.plan_digest(
            tenant=self.tenant_ref or "",
            environment_generation=self.environment_generation or "",
            plan_id=self.id, plan_version=self.version,
            fact_snapshot_hash=self.fact_snapshot_hash or "",
            rule_version=self.rule_version or 1, actions=actions)

    def _forgeflow_approval(self, *, expected_digest):
        self.ensure_one()
        decision = self.env["forgeflow.decision_record"].search(
            [("plan_id", "=", self.id), ("decision", "=", "approved")],
            order="decided_at desc, id desc", limit=1)
        record = None
        if decision:
            self.env.cr.execute("SELECT id FROM forgeflow_decision_record WHERE id=%s FOR SHARE",
                                (decision.id,))
            decision.invalidate_recordset()
            approver = decision.approver_id
            # Approval is a current fact, not a historical one: a disabled
            # account or a withdrawn approval stops the execution now.
            permitted = permission(approver, "approver", plan_id=self.id)
            if not decision.caller_id or (decision.caller_id != approver
                                           and not decision.delegation_id):
                raise UserError("approval_caller_unverified")
            if approver and self.company_id not in approver.company_ids:
                raise UserError("approval_approver_company_denied")
            if decision.delegation_id:
                decision.delegation_id._forgeflow_validate(
                    self, approver.id, decision.caller_id.id)
            record = {
                "decision": decision.decision,
                "plan_hash": decision.plan_hash,
                "plan_version": decision.plan_version,
                "plan_digest": decision.plan_digest,
                "action_digest": decision.action_digest,
                "environment_generation": decision.environment_generation,
                "approver_id": approver.id if approver else None,
                "approver_active": bool(approver.active) if approver else False,
                "approver_permitted": permitted,
                "execution_identity": decision.execution_identity,
                "expires_at": decision.expires_at,
                "revoked_at": decision.revoked_at,
            }
        try:
            return recovery_logic.validate_approval(
                plan={"plan_hash": self.plan_hash, "version": self.version,
                      "environment_generation": self.environment_generation},
                decision=record, expected_digest=expected_digest,
                expected_action_digest=self.action_digest)
        except recovery_logic.PlanError as error:
            raise UserError(error.code) from None

    def _forgeflow_receipt(self, operation):
        receipt = self.env["forgeflow.execution_receipt"].search(
            [("operation_id", "=", operation.id)], order="id desc", limit=1)
        return {
            "operation_id": operation.id,
            "state": operation.state,
            "readback": self._forgeflow_loads(receipt.readback_json) if receipt else None,
            "receipt_id": receipt.id if receipt else None,
        }

    @staticmethod
    def _forgeflow_loads(raw):
        try:
            return json.loads(raw) if raw else None
        except ValueError:
            return None

    def _forgeflow_rule_scope(self, actions, snapshot):
        """What this plan actually depends on: company, models and references.

        A rule for another model, another company or another document is not a
        dependency.  The scope is stored with the plan so a later rule change can
        be judged against it instead of a global digest.
        """
        models = sorted({str(action["target_model"]) for action in actions})
        references = {str(item) for item in (snapshot.get("affected_targets") or [])}
        for action in actions:
            if int(action.get("target_id") or 0) > 0:
                references.add(f"{action['target_model']}:{int(action['target_id'])}")
        order = snapshot.get("order") if isinstance(snapshot.get("order"), dict) else {}
        if order.get("name"):
            references.add(str(order["name"]))
        if order.get("id"):
            references.add(f"purchase.order:{int(order['id'])}")
        for line in snapshot.get("lines") or []:
            if isinstance(line, dict) and line.get("name"):
                references.add(str(line["name"]))
        for entry in (snapshot.get("relations") or {}).get("direct") or []:
            if isinstance(entry, dict) and entry.get("ref"):
                references.add(str(entry["ref"]))
            if isinstance(entry, dict) and entry.get("name"):
                references.add(str(entry["name"]))
        # ``self`` is empty when ``forgeflow_prepare`` is called as a model
        # method; fall back to the environment company in that case.
        company = self.company_id if self else self.env.company
        return {"company_id": company.id or self.env.company.id or 0,
                "models": models,
                "refs": sorted(reference for reference in references if reference)}

    def _forgeflow_current_rule_epoch(self):
        rules = self.env["forgeflow.business_rule"]
        scope = self._forgeflow_loads(self.rule_scope_json) if self.rule_scope_json else None
        if isinstance(scope, dict) and scope.get("models"):
            return rules.forgeflow_rule_epoch_for(scope)
        # Historical plans have no recorded scope; keep their original meaning.
        return rules.forgeflow_rule_epoch()

    def _forgeflow_check_expiry(self):
        """A plan past its own validity window is not an executable approval."""
        if not self.expires_at:
            return None
        moment = fields.Datetime.to_datetime(self.expires_at)
        if moment is None or moment >= fields.Datetime.now():
            return None
        self.write({"state": "expired"})
        return {"plan_id": self.id, "state": "expired", "error": "plan_expired",
                "expires_at": str(self.expires_at)}

    # ----------------------------------------------------------- preparation
    @controlled("planner", "service")
    def forgeflow_prepare(self, exception_id, actions, fact_snapshot,
                          environment_generation, *, rule_version=1, tenant_ref="",
                          name="Recovery plan", expires_in_hours=72):
        """Create a plan whose digest binds the complete action set."""
        self.ensure_one() if self else None
        try:
            validated = recovery_logic.execution_order(actions)
        except recovery_logic.PlanError as error:
            raise UserError(error.code) from None
        exception = self.env["forgeflow.exception_event"].browse(int(exception_id)).exists()
        if not exception or exception.company_id != self.env.company:
            raise AccessError("exception_company_mismatch")
        exception.check_access("read")
        snapshot = fact_snapshot or {}
        snapshot_hash = recovery_logic.digest(snapshot)
        canonical = {
            "actions": validated,
            "fact_snapshot_hash": snapshot_hash,
            "environment_generation": environment_generation,
            "rule_version": int(rule_version),
            "tenant_ref": tenant_ref,
        }
        plan_hash = recovery_logic.digest(canonical)
        rule_scope = self._forgeflow_rule_scope(validated, snapshot)
        epoch = self.env["forgeflow.business_rule"].forgeflow_rule_epoch_for(
            rule_scope) if rule_scope else (
                self.env["forgeflow.business_rule"].forgeflow_rule_epoch())
        plan = self.create({
            "name": name,
            "exception_id": exception_id,
            "company_id": self.env.company.id,
            "plan_hash": plan_hash,
            "rule_epoch": epoch,
            "rule_scope_json": json.dumps(rule_scope, sort_keys=True),
            "action_digest": recovery_logic.action_digest(validated),
            "action_count": len(validated),
            "fact_snapshot_hash": snapshot_hash,
            "fact_snapshot_json": json.dumps(snapshot, ensure_ascii=False, default=str),
            "environment_generation": environment_generation,
            "tenant_ref": tenant_ref,
            "rule_version": int(rule_version),
            "payload_json": json.dumps({**canonical, "actions": validated},
                                       ensure_ascii=False, default=str),
            "state": "awaiting_approval",
            "expires_at": fields.Datetime.now() + __import__("datetime").timedelta(
                hours=int(expires_in_hours)),
        })
        plan.write({"plan_digest": plan._forgeflow_digest()})
        return plan.id

    @controlled("approver", "service")
    def forgeflow_approve(self, approver_id, execution_identity, *, expires_in_hours=24,
                          comment="", delegation_id=None, expected_plan_version=None,
                          expected_plan_digest=None, expected_action_digest=None):
        """Record an approval bound to the current plan digest."""
        self.ensure_one()
        # Lock before re-reading the review identity; concurrent edits/approvals
        # cannot bind a decision to content the human did not see.
        self._forgeflow_lock()
        self._forgeflow_check_review(expected_plan_version, expected_plan_digest,
                                     expected_action_digest)
        approver = self.env["res.users"].browse(int(approver_id)).exists()
        if (not permission(approver, "approver", plan_id=self.id)
                or self.company_id not in approver.company_ids):
            raise AccessError("approver_permission_denied")
        delegation = self.env["forgeflow.approval_delegation"]
        if self.env.uid != approver.id:
            if not permission(self.env.user, "service", plan_id=self.id) or not delegation_id:
                raise AccessError("approval_delegation_required")
            delegation = delegation.browse(int(delegation_id)).exists()
            if not delegation:
                raise AccessError("approval_delegation_required")
            delegation._forgeflow_validate(self, approver.id, self.env.uid)
        if not execution_identity:
            raise UserError("execution_identity_required")
        if self._forgeflow_check_expiry():
            raise UserError("plan_expired")
        if self.state not in ("awaiting_approval", "draft"):
            raise UserError("plan_not_awaiting_approval")
        digest = self._forgeflow_digest()
        decision = self.env["forgeflow.decision_record"].create({
            "plan_id": self.id,
            "decision": "approved",
            "approver_id": int(approver_id),
            "caller_id": self.env.uid,
            "delegation_id": delegation.id or False,
            "plan_hash": self.plan_hash,
            "plan_version": self.version,
            "plan_digest": digest,
            "action_digest": self.action_digest,
            "environment_generation": self.environment_generation,
            "execution_identity": execution_identity,
            "expires_at": fields.Datetime.now() + __import__("datetime").timedelta(
                hours=int(expires_in_hours)),
            "comment": comment,
        })
        self.write({"state": "approved", "plan_digest": digest})
        self.exception_id.write({"state": "awaiting_approval"})
        return decision.id

    def _forgeflow_check_review(self, version, plan_digest, action_digest):
        facts = self._forgeflow_loads(self.fact_snapshot_json) or {}
        if not isinstance(facts, dict):
            raise UserError("plan_review_stale")
        completeness = facts.get("completeness")
        if completeness is not None and (
                not isinstance(completeness, dict)
                or completeness.get("complete") is False
                or completeness.get("truncated") is True
                or completeness.get("lines_complete") is False
                or completeness.get("query_failures")
                or completeness.get("missing_relations")):
            raise UserError("recovery_plan_snapshot_incomplete")
        if (not version or not plan_digest or not action_digest
                or version != self.version or plan_digest != self._forgeflow_digest()
                or action_digest != self.action_digest
                or action_digest != recovery_logic.action_digest(self._forgeflow_actions())
                or self.fact_snapshot_hash != recovery_logic.digest(
                    self._forgeflow_loads(self.fact_snapshot_json) or {})):
            raise UserError("plan_review_stale")

    @controlled("approver")
    def forgeflow_delegate_approval(self, service_user_id, *, expected_plan_version,
                                    expected_plan_digest, expected_action_digest,
                                    expires_in_hours=1):
        self.ensure_one()
        self._forgeflow_check_review(expected_plan_version, expected_plan_digest,
                                     expected_action_digest)
        if not 0 < float(expires_in_hours) <= 24:
            raise UserError("delegation_expiry_invalid")
        service = self.env["res.users"].browse(int(service_user_id)).exists()
        if (not permission(service, "service", plan_id=self.id)
                or self.company_id not in service.company_ids):
            raise AccessError("delegation_service_denied")
        grant = self.env["forgeflow.approval_delegation"].create({
            "plan_id": self.id, "approver_id": self.env.uid,
            "service_user_id": service.id, "plan_version": self.version,
            "plan_digest": self._forgeflow_digest(), "action_digest": self.action_digest,
            "expires_at": fields.Datetime.now() + __import__("datetime").timedelta(
                hours=float(expires_in_hours))})
        return grant.id

    # ------------------------------------------------------------- execution
    def _forgeflow_action(self, action_id):
        for action in self._forgeflow_actions():
            if action["action_id"] == str(action_id):
                return action
        raise UserError("action_not_in_approved_plan")

    # --------------------------------------------------------- execution core
    def _forgeflow_operation_key(self, execution_key, action_id):
        return f"plan:{execution_key}:{action_id}"

    def _forgeflow_plan_key(self, digest, execution_identity):
        return recovery_logic.plan_execution_key(
            plan_id=self.id, plan_version=self.version, plan_digest=digest,
            execution_identity=execution_identity,
            environment_generation=self.environment_generation or "")

    def _forgeflow_register_operation(self, action, execution_key, digest, approval,
                                      sequence, idempotency_key=None):
        """Persist the attempt before any business write is attempted."""
        fingerprint = recovery_logic.idempotency_fingerprint(
            company_id=self.company_id.id or 0,
            environment_generation=self.environment_generation or "",
            plan_id=self.id, plan_version=self.version,
            action_id=action["action_id"], params=action.get("params") or {},
            execution_identity=approval["execution_identity"])
        return self.env["forgeflow.execution_operation"].create({
            "plan_id": self.id,
            "plan_version": self.version,
            "plan_digest": digest,
            "action_id": action["action_id"],
            "operation_type": action["type"],
            "target_model": action["target_model"],
            "target_id": int(action["target_id"]),
            "request_hash": recovery_logic.digest(action),
            "fingerprint": fingerprint,
            "environment_generation": self.environment_generation,
            "execution_identity": approval["execution_identity"],
            "execution_key": execution_key,
            "sequence": int(sequence),
            "idempotency_key": (idempotency_key
                                or self._forgeflow_operation_key(execution_key,
                                                                 action["action_id"])),
            "state": "dispatched",
            "attempts": 1,
            "dispatched_at": fields.Datetime.now(),
            "request_json": json.dumps(action, ensure_ascii=False, default=str),
        })

    @staticmethod
    def _forgeflow_created_entry(action, readback):
        entry = dict(readback or {})
        entry.setdefault("model", action["target_model"])
        return entry

    def _forgeflow_plan_replay(self, operations):
        actions = [{"operation_id": operation.id, "action_id": operation.action_id,
                    "idempotency_key": operation.idempotency_key,
                    "state": operation.state,
                    "readback": self._forgeflow_loads(operation.response_json)}
                   for operation in operations]
        states = {operation.state for operation in operations}
        if states and states <= {"succeeded", "verified"}:
            return {"plan_id": self.id, "state": "executed", "actions": actions}
        if "failed" in states:
            return {"plan_id": self.id, "state": "execution_failed",
                    "error": self.execution_error or "execution_failed",
                    "rolled_back": True, "actions": actions}
        return {"plan_id": self.id, "state": "reconcile_required",
                "actions": actions}

    def _forgeflow_fail_plan(self, operations, code):
        """Record the attempt after the business savepoint has rolled back."""
        now = fields.Datetime.now()
        self.write({"state": "execution_failed", "execution_error": code})
        self.exception_id.write({"state": "needs_action"})
        for operation in operations:
            operation.write({"state": "failed", "completed_at": now,
                             "response_json": json.dumps(
                                 {"error": code, "rolled_back": True},
                                 ensure_ascii=False)})
            self.env["forgeflow.execution_receipt"].create({
                "operation_id": operation.id, "state": "failed",
                "readback_json": json.dumps({
                    "error": code, "rolled_back": True,
                    "business_changes": "rolled_back"}, ensure_ascii=False)})

    def _forgeflow_object_ref(self):
        """The business object this plan is about, for the outbox event."""
        facts = self._forgeflow_loads(self.fact_snapshot_json) or {}
        targets = facts.get("affected_targets") or []
        if targets:
            return str(targets[0])
        return f"forgeflow.recovery_plan:{self.id}"

    def _forgeflow_complete_plan(self, operations):
        now = fields.Datetime.now()
        for operation in operations:
            readback = self._forgeflow_loads(operation.response_json) or {}
            self.env["forgeflow.execution_receipt"].create({
                "operation_id": operation.id, "state": "succeeded",
                "readback_json": json.dumps(readback, ensure_ascii=False,
                                            default=str)})
            self.env["forgeflow.event_outbox"].create({
                "event_type": "execution.applied",
                "aggregate_model": "forgeflow.execution_operation",
                "aggregate_id": operation.id,
                "external_event_id": (f"apply:{operation.execution_key}:"
                                      f"{operation.action_id}"),
                "payload_json": json.dumps({
                    "operation_id": operation.id,
                    "action_id": operation.action_id,
                    "plan_id": self.id,
                    "object_ref": (f"{operation.target_model}:{operation.target_id}"
                                   if operation.target_id
                                   else self._forgeflow_object_ref()),
                }, ensure_ascii=False)})
        self.write({"state": "executed", "execution_error": False,
                    "executed_at": now})

    def _forgeflow_executed_actions(self):
        """The action set with earlier-created references resolved, if known."""
        resolved_actions = []
        for action in self._forgeflow_actions():
            operation = self.env["forgeflow.execution_operation"].search([
                ("plan_id", "=", self.id),
                ("execution_key", "=", self.execution_key), ("plan_version", "=", self.version),
                ("action_id", "=", action["action_id"]),
                ("state", "in", ("succeeded", "verified"))],
                order="id desc", limit=1)
            payload = self._forgeflow_loads(operation.request_json) if operation else None
            resolved_actions.append(payload if isinstance(payload, dict) else action)
        return resolved_actions

    @controlled("executor", "service")
    def forgeflow_apply_action(self, action_id, idempotency_key, execution_identity=None):
        """Apply the single action of a one-action plan through the legacy entry.

        A multi-action plan has one atomic boundary.  Running one of its actions
        here would commit an undeclared partial change, so the entry refuses it:
        the whole plan must run through ``forgeflow_apply_plan``.

        ``execution_identity`` is optional for the internal path (the approval's
        own identity is used); an explicit value must match the approval.
        """
        self.ensure_one()
        self._forgeflow_lock()
        expired = self._forgeflow_check_expiry()
        if expired is not None:
            return clean_for_rpc(expired)
        stale = self._forgeflow_check_rule_epoch()
        if stale is not None:
            return clean_for_rpc(stale)
        if not idempotency_key or not action_id:
            raise UserError("invalid_operation")
        actions = self._forgeflow_actions()
        action = self._forgeflow_action(action_id)
        digest = self._forgeflow_digest()
        # Approval is the first gate: an unapproved plan reports that, not a
        # policy about how its actions are grouped.
        approval = self._forgeflow_approval(expected_digest=digest)
        if len(actions) > 1:
            raise UserError("whole_plan_execution_required")
        if execution_identity and approval["execution_identity"] != execution_identity:
            raise UserError("execution_identity_mismatch")
        key = self._forgeflow_plan_key(digest, approval["execution_identity"])
        if self.execution_key and self.execution_key != key:
            raise UserError("execution_identity_conflict")
        fingerprint = recovery_logic.idempotency_fingerprint(
            company_id=self.company_id.id or 0,
            environment_generation=self.environment_generation or "",
            plan_id=self.id, plan_version=self.version,
            action_id=action["action_id"], params=action.get("params") or {},
            execution_identity=approval["execution_identity"])
        by_key = self.env["forgeflow.execution_operation"].search(
            [("idempotency_key", "=", idempotency_key)], limit=1)
        if by_key:
            if by_key.fingerprint != fingerprint:
                raise UserError("idempotency_key_conflict")
            return clean_for_rpc(self._forgeflow_receipt(by_key))
        previous = self.env["forgeflow.execution_operation"].search([
            ("plan_id", "=", self.id), ("execution_key", "=", key),
            ("action_id", "=", action["action_id"])], order="sequence, id", limit=1)
        if previous:
            # The same plan attempt under a different caller key answers from
            # the original operation; it never becomes a second write.
            return clean_for_rpc(self._forgeflow_receipt(previous))
        if not self.execution_key:
            self.write({"execution_key": key,
                        "execution_attempts": (self.execution_attempts or 0) + 1})
        operation = self._forgeflow_register_operation(
            action, key, digest, approval, 0, idempotency_key=idempotency_key)
        self.env.flush_all()
        try:
            with self.env.cr.savepoint():
                readback = self._forgeflow_run_action(action, operation)
        except Exception as error:  # noqa: BLE001 - every failure becomes a receipt
            code = recovery_logic.failure_code(error)
            self._forgeflow_fail_plan([operation], code)
            return clean_for_rpc({**self._forgeflow_receipt(operation),
                                  "rolled_back": True, "error": code,
                                  "plan_state": "execution_failed"})
        operation.write({"state": "succeeded", "completed_at": fields.Datetime.now(),
                         "response_json": json.dumps(readback, ensure_ascii=False,
                                                     default=str)})
        self.write({"state": "executed", "executed_at": fields.Datetime.now(),
                    "execution_error": False})
        receipt = self.env["forgeflow.execution_receipt"].create({
            "operation_id": operation.id, "state": "succeeded",
            "readback_json": json.dumps(readback, ensure_ascii=False, default=str),
        })
        self.env["forgeflow.event_outbox"].create({
            "event_type": "execution.applied",
            "aggregate_model": "forgeflow.execution_operation",
            "aggregate_id": operation.id,
            "external_event_id": f"apply:{key}:{action['action_id']}",
            "payload_json": json.dumps({"operation_id": operation.id,
                                        "action_id": action["action_id"]},
                                       ensure_ascii=False),
        })
        return clean_for_rpc({"operation_id": operation.id, "state": "succeeded",
                              "action_id": action["action_id"],
                              "readback": readback, "receipt_id": receipt.id})

    @controlled("executor", "service")
    def forgeflow_apply(self, idempotency_key, operation_type, target_model, target_id,
                        payload=None):
        """Compatibility entry point: resolve the approved action, never trust params.

        The caller may name *which* action it means, but the record, the quantity,
        the dates and the currency all come from the approved plan.  A payload that
        disagrees with the approved action is rejected.
        """
        self.ensure_one()
        if not idempotency_key or not operation_type or not target_model:
            raise UserError("invalid_operation")
        matches = [action for action in self._forgeflow_actions()
                   if action["type"] == operation_type
                   and action["target_model"] == target_model
                   and int(action["target_id"]) == int(target_id)]
        if not matches:
            raise UserError("target_not_in_approved_plan")
        if len(matches) > 1:
            raise UserError("action_ambiguous_use_action_id")
        action = matches[0]
        if payload:
            approved = recovery_logic.canonical_action(action)["params"]
            supplied = {key: value for key, value in payload.items() if value is not None}
            unexpected = {key: value for key, value in supplied.items()
                          if key not in approved}
            if unexpected:
                raise UserError("parameters_not_in_approved_plan")
            changed = {key: value for key, value in supplied.items()
                       if approved.get(key) != value}
            if changed:
                raise UserError("parameters_not_in_approved_plan")
        return self.forgeflow_apply_action(action["action_id"], idempotency_key)

    def _forgeflow_check_rule_epoch(self):
        """A confirmed rule change invalidates approvals prepared before it.

        Returns a rejection instead of raising: the caller must keep the ``stale``
        mark, and raising inside the same transaction would roll it back.
        """
        self.ensure_one()
        if not self.rule_epoch:
            return None
        current = self._forgeflow_current_rule_epoch()
        if current == self.rule_epoch:
            return None
        self.write({"state": "stale"})
        return {"plan_id": self.id, "state": "stale", "error": "rules_changed",
                "rule_epoch": self.rule_epoch, "current_epoch": current}

    @controlled("executor", "service")
    def forgeflow_apply_plan(self, idempotency_prefix, execution_identity):
        """Execute the whole approved plan inside one business savepoint.

        The protocol is: persist the attempts, then execute every action inside a
        single savepoint, then record the result.  A failure rolls the business
        writes back and the failure receipt is written *after* that rollback, so
        the two cannot disappear together.  When the connection itself is lost,
        nothing in this transaction survives, including this receipt: the control
        layer keeps the attempt and reconciles it separately.  No failure can
        promise a receipt from a dead transaction.
        """
        self.ensure_one()
        self._forgeflow_lock()
        if not idempotency_prefix:
            raise UserError("idempotency_prefix_required")
        expired = self._forgeflow_check_expiry()
        if expired is not None:
            return clean_for_rpc(expired)
        stale = self._forgeflow_check_rule_epoch()
        if stale is not None:
            return clean_for_rpc(stale)
        digest = self._forgeflow_digest()
        approval = self._forgeflow_approval(expected_digest=digest)
        if approval["execution_identity"] != execution_identity:
            raise UserError("execution_identity_mismatch")
        actions = self._forgeflow_actions()
        key = self._forgeflow_plan_key(digest, execution_identity)
        if self.execution_key and self.execution_key != key:
            raise UserError("execution_identity_conflict")
        existing = self.env["forgeflow.execution_operation"].search(
            [("plan_id", "=", self.id), ("execution_key", "=", key)],
            order="sequence, id")
        if existing:
            # A retry, however its prefix changed, reads the original attempt.
            return clean_for_rpc(self._forgeflow_plan_replay(existing))
        self.write({"execution_key": key,
                    "execution_attempts": (self.execution_attempts or 0) + 1,
                    "state": "executing", "execution_error": False})
        self.exception_id.write({"state": "executing"})
        operations = [self._forgeflow_register_operation(
            action, key, digest, approval, sequence)
            for sequence, action in enumerate(actions)]
        # Flush the attempt rows *before* the savepoint so a rollback of the
        # business writes cannot also erase the record that they were tried.
        self.env.flush_all()
        try:
            with self.env.cr.savepoint():
                created = {}
                for action, operation in zip(actions, operations):
                    resolved = recovery_logic.resolve_action_references(action, created)
                    operation.write({
                        "target_id": int(resolved["target_id"]),
                        "request_json": json.dumps(resolved, ensure_ascii=False,
                                                   default=str)})
                    readback = self._forgeflow_run_action(resolved, operation)
                    created[action["action_id"]] = self._forgeflow_created_entry(
                        resolved, readback)
                    operation.write({
                        "state": "succeeded",
                        "completed_at": fields.Datetime.now(),
                        "response_json": json.dumps(readback, ensure_ascii=False,
                                                    default=str)})
        except Exception as error:  # noqa: BLE001 - business and database failures
            code = recovery_logic.failure_code(error)
            self._forgeflow_fail_plan(operations, code)
            return clean_for_rpc({
                "plan_id": self.id, "state": "execution_failed", "error": code,
                "rolled_back": True,
                "actions": [{"action_id": operation.action_id,
                             "idempotency_key": operation.idempotency_key,
                             "state": "rolled_back"} for operation in operations]})
        self._forgeflow_complete_plan(operations)
        return clean_for_rpc({
            "plan_id": self.id, "state": "executed",
            "actions": [{"operation_id": operation.id,
                         "action_id": operation.action_id,
                         "idempotency_key": operation.idempotency_key,
                         "state": operation.state,
                         "readback": self._forgeflow_loads(operation.response_json)}
                        for operation in operations]})

    # --------------------------------------------------------------- actions
    def _forgeflow_run_action(self, action, operation):
        kind = action["type"]
        handler = getattr(self, f"_forgeflow_{kind}", None)
        if handler is None:
            raise UserError("operation_not_supported")
        return handler(action)

    def _forgeflow_read(self, model, record_id, fields_list):
        record = self.env[model].browse(int(record_id)).exists()
        if not record:
            raise UserError("target_not_found")
        self._forgeflow_check_company(record)
        return record, {name: self._forgeflow_value(record, name) for name in fields_list}

    def _forgeflow_check_company(self, record, *, allow_shared=False):
        """A multi-company caller still acts only within this plan's company."""
        record.check_access("read")
        if "company_id" not in record._fields:
            raise UserError("target_company_unavailable")
        if record.company_id != self.company_id and not (
                allow_shared and not record.company_id):
            raise UserError("target_company_mismatch")

    @staticmethod
    def _forgeflow_value(record, name):
        value = record[name]
        if isinstance(value, models.BaseModel):
            return value.id
        if isinstance(value, models.BaseModel.__mro__[0]):
            return value.id
        return value

    def _forgeflow_preconditions(self, action, current):
        try:
            recovery_logic.check_preconditions(action, current)
        except recovery_logic.PlanError as error:
            raise UserError(error.code) from None

    def _forgeflow_preserve_supply(self, action):
        model = action["target_model"]
        record, current = self._forgeflow_read(model, action["target_id"], ["id", "state"])
        self._forgeflow_preconditions(action, current)
        return {"model": model, "id": record.id, "state": current["state"],
                "preserved": True}

    def _forgeflow_cancel_purchase_order(self, action):
        record, current = self._forgeflow_read("purchase.order", action["target_id"],
                                               ["id", "state", "name"])
        self._forgeflow_preconditions(action, current)
        if current["state"] not in ("purchase", "draft", "sent", "to approve"):
            raise UserError("target_not_cancellable")
        record.button_cancel()
        return {"model": "purchase.order", "id": record.id, "state": record.state}

    def _forgeflow_cancel_manufacturing_order(self, action):
        record, current = self._forgeflow_read("mrp.production", action["target_id"],
                                               ["id", "state", "name"])
        self._forgeflow_preconditions(action, current)
        if current["state"] in ("done", "cancel"):
            raise UserError("target_not_cancellable")
        record.action_cancel()
        return {"model": "mrp.production", "id": record.id, "state": record.state}

    def _forgeflow_adjust_purchase_line(self, action):
        record, current = self._forgeflow_read(
            "purchase.order.line", action["target_id"],
            ["id", "product_qty", "date_planned", "order_id"])
        self._forgeflow_preconditions(action, current)
        values = {"product_qty": action["quantity"]}
        params = action.get("params") or {}
        if params.get("date_planned"):
            values["date_planned"] = params["date_planned"]
        record.write(values)
        return {"model": "purchase.order.line", "id": record.id,
                "product_qty": record.product_qty,
                "date_planned": str(record.date_planned)}

    def _forgeflow_create_purchase_order(self, action):
        params = action.get("params") or {}
        partner_id = int(params.get("partner_id") or 0)
        if not partner_id:
            raise UserError("partner_required")
        if not params.get("lines"):
            raise UserError("purchase_lines_required")
        # A creation action has no existing record whose current value could be
        # compared, so its dependencies are checked now: the vendor must exist,
        # the currency must be the plan company's, and every product must exist
        # and be active.
        partner = self.env["res.partner"].browse(partner_id).exists()
        if not partner:
            raise UserError("partner_not_found")
        self._forgeflow_check_company(partner, allow_shared=True)
        if not partner.active:
            raise UserError("partner_inactive")
        currency_name = str(action.get("currency") or "")
        if not currency_name:
            raise UserError("currency_required")
        currency = self.env["res.currency"].search(
            [("name", "=", currency_name)], limit=1)
        if not currency:
            raise UserError("currency_unknown")
        company_currency = self.company_id.currency_id
        if company_currency and currency != company_currency:
            raise UserError("currency_mismatch")
        checked_products = []
        for line in params["lines"]:
            product = self.env["product.product"].browse(
                int(line.get("product_id") or 0)).exists()
            if not product:
                raise UserError("product_not_found")
            self._forgeflow_check_company(product, allow_shared=True)
            if not product.active:
                raise UserError("product_inactive")
            if float(line.get("product_qty") or 0) <= 0:
                raise UserError("quantity_required")
            if float(line.get("price_unit") or 0) < 0:
                raise UserError("price_invalid")
            checked_products.append(product.id)
        order = self.env["purchase.order"].create({
            "partner_id": partner_id,
            "company_id": self.company_id.id,
            "date_planned": params.get("date_planned") or fields.Datetime.now(),
            "order_line": [(0, 0, {
                "product_id": int(line["product_id"]),
                "product_qty": line["product_qty"],
                "price_unit": line.get("price_unit") or 0.0,
                "date_planned": line.get("date_planned") or params.get("date_planned")
                or fields.Datetime.now(),
            }) for line in params["lines"]],
        })
        if params.get("confirm", True):
            order.button_confirm()
        result = {"model": "purchase.order", "id": order.id, "state": order.state,
                  "name": order.name, "date_planned": str(order.date_planned),
                  "dependency_checks": {
                      "partner_id": partner.id, "partner_active": bool(partner.active),
                      "currency": currency.name,
                      "company_currency": company_currency.name if company_currency else "",
                      "products": checked_products}}
        line = order.order_line[:1]
        if line:
            # A later action can name this line instead of guessing its id.
            result["line_id"] = line.id
            result["line_id_model"] = "purchase.order.line"
        return result

    def _forgeflow_adjust_manufacturing_order(self, action):
        record, current = self._forgeflow_read(
            "mrp.production", action["target_id"],
            ["id", "product_qty", "date_start", "date_finished"])
        self._forgeflow_preconditions(action, current)
        values = {"product_qty": action["quantity"]}
        params = action.get("params") or {}
        for key in ("date_start", "date_finished"):
            if params.get(key):
                values[key] = params[key]
        record.write(values)
        return {"model": "mrp.production", "id": record.id,
                "product_qty": record.product_qty,
                "date_start": str(record.date_start),
                "date_finished": str(record.date_finished)}

    def _forgeflow_create_manufacturing_order(self, action):
        params = action.get("params") or {}
        product_id = int(params.get("product_id") or 0)
        if not product_id:
            raise UserError("product_required")
        # Same rule as a purchase creation: the plan's dependencies are checked
        # against the current database, not only at preparation time.
        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            raise UserError("product_not_found")
        self._forgeflow_check_company(product, allow_shared=True)
        if not product.active:
            raise UserError("product_inactive")
        if float(action.get("quantity") or 0) <= 0:
            raise UserError("quantity_required")
        bom = None
        if params.get("bom_id"):
            bom = self.env["mrp.bom"].browse(int(params["bom_id"])).exists()
            if not bom:
                raise UserError("bom_not_found")
            self._forgeflow_check_company(bom, allow_shared=True)
            if "active" in bom._fields and not bom.active:
                raise UserError("bom_inactive")
            if bom.product_tmpl_id and bom.product_tmpl_id != product.product_tmpl_id:
                raise UserError("bom_product_mismatch")
        workcenter = None
        if params.get("workcenter_id"):
            workcenter = self.env["mrp.workcenter"].browse(
                int(params["workcenter_id"])).exists()
            if not workcenter:
                raise UserError("workcenter_not_found")
            self._forgeflow_check_company(workcenter, allow_shared=True)
        values = {
            "product_id": product_id,
            "company_id": self.company_id.id,
            "product_qty": action["quantity"],
            "date_start": params.get("date_start") or fields.Datetime.now(),
        }
        if bom is not None:
            values["bom_id"] = bom.id
        if params.get("date_finished"):
            values["date_finished"] = params["date_finished"]
        if workcenter is not None and "workcenter_id" in self.env[
                "mrp.production"]._fields:
            values["workcenter_id"] = workcenter.id
        order = self.env["mrp.production"].create(values)
        if params.get("confirm", True):
            order.action_confirm()
        result = {"model": "mrp.production", "id": order.id, "state": order.state,
                  "name": order.name, "product_qty": order.product_qty,
                  "dependency_checks": {
                      "product_id": product.id, "product_active": bool(product.active),
                      "bom_id": bom.id if bom is not None else None,
                      "workcenter_id": (workcenter.id if workcenter is not None
                                        else None)}}
        finished = order.move_finished_ids[:1]
        if finished:
            result["move_id"] = finished.id
            result["move_id_model"] = "stock.move"
        return result

    CONFLICT_MARKERS = ("SerializationFailure", "DeadlockDetected",
                        "could not serialize", "deadlock detected")

    def _forgeflow_reserve_stock(self, action):
        """Reserve stock all or nothing; a concurrent loss is a business conflict.

        Two plans fighting for the same units produce a PostgreSQL serialisation
        failure.  That is the *expected* outcome for the loser, but a raw database
        error is not an answer a planner can act on, so it is translated into
        ``stock_conflict``: re-read the facts and re-plan.
        """
        try:
            return self._forgeflow_reserve_stock_locked(action)
        except Exception as error:  # noqa: BLE001 - classified below
            text = f"{type(error).__name__}: {error}"
            if any(marker in text for marker in self.CONFLICT_MARKERS):
                raise UserError("stock_conflict") from None
            raise

    def _forgeflow_reserve_stock_locked(self, action):
        """Reserve stock all-or-nothing, serialised on the quant rows.

        A partial reservation that silently succeeds is how two recovery plans
        both "get" the same ten units.  The quants are locked first, availability
        is re-read under the lock, and a shortfall raises so the caller's
        savepoint rolls the whole action back.
        """
        params = action.get("params") or {}
        product_id = int(params.get("product_id") or 0)
        if not product_id:
            raise UserError("product_required")
        quantity = float(action.get("quantity") or 0)
        if quantity <= 0:
            raise UserError("quantity_required")
        location_id = int(params.get("location_id") or 0)
        move_id = params.get("move_id")
        product = self.env["product.product"].browse(product_id).exists()
        if not product:
            raise UserError("product_not_found")
        self._forgeflow_check_company(product, allow_shared=True)
        if not product.active:
            raise UserError("product_inactive")
        location = None
        if location_id:
            location = self.env["stock.location"].browse(location_id).exists()
            if not location:
                raise UserError("location_not_found")
            self._forgeflow_check_company(location, allow_shared=True)
        move = None
        if move_id:
            move = self.env["stock.move"].browse(int(move_id)).exists()
            if not move:
                raise UserError("target_not_found")
            self._forgeflow_check_company(move)

        # Serialise competing reservations on the quant rows for this product.
        company_id = self.company_id.id or 0
        query = (
            "SELECT q.id FROM stock_quant q "
            "JOIN stock_location l ON l.id = q.location_id "
            "WHERE q.product_id = %s AND l.usage = 'internal' AND q.company_id = %s")
        args = [product_id, company_id]
        if location_id:
            query += " AND q.location_id = %s"
            args.append(location_id)
        query += " ORDER BY q.id FOR UPDATE"
        self.env.cr.execute(query, tuple(args))
        quant_ids = [row[0] for row in self.env.cr.fetchall()]
        if not quant_ids:
            raise UserError("stock_unavailable")
        quants = self.env["stock.quant"].browse(quant_ids)
        available = sum(quant.available_quantity for quant in quants)
        if available + 1e-9 < quantity:
            raise UserError("stock_unavailable")

        if move is None:
            location = location or quants[0].location_id
            self._forgeflow_check_company(location, allow_shared=True)
            destination = self.env.ref("stock.stock_location_customers")
            self._forgeflow_check_company(destination, allow_shared=True)
            move = self.env["stock.move"].create({
                # Odoo 20 derives the move description from the product; `name` is
                # no longer a writable field.
                "origin": action.get("summary") or "ForgeFlow recovery reservation",
                "product_id": product_id,
                "company_id": self.company_id.id,
                "product_uom_qty": quantity,
                "location_id": location.id,
                "location_dest_id": destination.id,
                "state": "confirmed",
            })
        move._action_assign()
        reserved = float(getattr(move, "quantity", 0) or 0)
        if reserved + 1e-9 < quantity:
            # The whole-plan savepoint rolls this back, releasing
            # whatever was partially reserved.
            raise UserError("stock_unavailable")
        return {"model": "stock.move", "id": move.id, "state": move.state,
                "reserved": reserved, "available_before": available}

    def _forgeflow_link_supply_to_demand(self, action):
        params = action.get("params") or {}
        sale_line_id = int(params.get("sale_line_id") or 0)
        if not sale_line_id:
            raise UserError("sale_line_required")
        sale_line = self.env["sale.order.line"].browse(sale_line_id).exists()
        if not sale_line:
            raise UserError("target_not_found")
        self._forgeflow_check_company(sale_line)
        model = action["target_model"]
        record, current = self._forgeflow_read(model, action["target_id"], ["id"])
        values = {"sale_line_id": sale_line.id}
        if "sale_id" in record._fields and sale_line.order_id:
            values["sale_id"] = sale_line.order_id.id
        record.write(values)
        return {"model": model, "id": record.id, "sale_line_id": record.sale_line_id.id,
                "sale_order": sale_line.order_id.name if sale_line.order_id else None}

    # ------------------------------------------------------- readback / verify
    @controlled("executor", "service")
    def forgeflow_readback(self):
        """Read every affected document back from the database, independently."""
        self.ensure_one()
        actions = self._forgeflow_executed_actions()
        result = {}
        for target in recovery_logic.readback_targets(actions):
            if target["kind"] == "created":
                operations = self.env["forgeflow.execution_operation"].search([
                    ("plan_id", "=", self.id), ("action_id", "=", target["action_id"]),
                    ("execution_key", "=", self.execution_key), ("plan_version", "=", self.version),
                    ("state", "in", ("succeeded", "verified"))], limit=1)
                if not operations:
                    continue
                created_id = self._forgeflow_loads(operations.response_json or "{}").get("id")
                if not created_id:
                    continue
                key = f"{target['model']}:new:{target['action_id']}"
                record = self.env[target["model"]].browse(int(created_id)).exists()
                result[key] = ({name: self._forgeflow_value(record, name)
                                for name in target["fields"] if name in record._fields}
                               | {"id": record.id})
                continue
            record = self.env[target["model"]].browse(int(target["id"])).exists()
            result[f"{target['model']}:{target['id']}"] = (
                {name: self._forgeflow_value(record, name)
                 for name in target["fields"] if name in record._fields}
                | {"id": record.id} if record else {"id": None, "missing": True})
        return clean_for_rpc(result)

    @controlled("executor", "service")
    def forgeflow_verify(self):
        """Layer three: does the executed business state meet the recovery goal?"""
        self.ensure_one()
        self._forgeflow_lock()
        if not self._forgeflow_execution_evidence_complete():
            return {"plan_id": self.id, "status": "unknown", "checks": [{
                "code": "execution_evidence_complete", "status": "unknown",
                "detail": "execution evidence incomplete; reconcile required"}]}
        self.write({"state": "verifying"})
        actions = self._forgeflow_executed_actions()
        readback = self.forgeflow_readback()
        checks = recovery_logic.business_acceptance(actions, readback)
        status = recovery_logic.acceptance_status(checks)
        self.env["forgeflow.execution_receipt"].create({
            "operation_id": (self.env["forgeflow.execution_operation"].search(
                [("plan_id", "=", self.id)], limit=1).id or 0),
            "state": status,
            "readback_json": json.dumps({"checks": checks, "readback": readback},
                                        ensure_ascii=False, default=str),
            "verified": status == "verified",
        })
        if status == "verified":
            self.write({"state": "executed"})
            self.exception_id.write({"state": "resolved"})
        elif status == "failed":
            self.write({"state": "execution_failed"})
            self.exception_id.write({"state": "needs_action"})
        return clean_for_rpc({"plan_id": self.id, "status": status, "checks": checks})

    def _forgeflow_execution_evidence_complete(self):
        operations = self.env["forgeflow.execution_operation"].search([
            ("plan_id", "=", self.id), ("execution_key", "=", self.execution_key)],
            order="id asc", limit=10_001)
        if operations:
            # A previous transaction may have changed only the operation while
            # this transaction waited for the plan lock. Under repeatable read,
            # lock the evidence rows too, so a stale snapshot aborts instead of
            # certifying the cached successful state.
            self.env.cr.execute(
                "SELECT id FROM forgeflow_execution_operation WHERE id IN %s ORDER BY id FOR SHARE",
                (tuple(operations.ids),))
            operations.invalidate_recordset()
        receipts = self.env["forgeflow.execution_receipt"].search([
            ("operation_id", "in", operations.ids)], order="id asc", limit=10_001)
        return recovery_logic.execution_evidence_complete(
            plan={"id": self.id, "version": self.version, "company_id": self.company_id.id,
                  "plan_digest": self._forgeflow_digest(), "execution_key": self.execution_key,
                  "environment_generation": self.environment_generation},
            actions=self._forgeflow_actions(),
            operations=operations.read([
                "id", "plan_id", "company_id", "plan_version", "plan_digest", "action_id",
                "execution_key", "execution_identity", "environment_generation", "operation_type",
                "state", "target_model", "target_id", "fingerprint", "request_hash", "response_json"]),
            receipts=receipts.read([
                "id", "operation_id", "company_id", "state", "verified", "readback_json"]))

    def forgeflow_plan_checks(self):
        self.ensure_one()
        self.check_access("read")
        facts = self._forgeflow_loads(self.fact_snapshot_json) or {}
        checks = recovery_logic.plan_checks(actions=self._forgeflow_actions(),
                                            facts=facts)
        return clean_for_rpc({"plan_id": self.id,
                            "status": recovery_logic.acceptance_status(checks),
                            "checks": checks})

    def forgeflow_reconcile(self, idempotency_key):
        """Read the original operation; never dispatch another native write."""
        self.ensure_one()
        self.check_access("read")
        operation = self.env["forgeflow.execution_operation"].search(
            [("idempotency_key", "=", idempotency_key), ("plan_id", "=", self.id)], limit=1)
        if not operation:
            raise UserError("operation_not_found")
        receipt = self.env["forgeflow.execution_receipt"].search(
            [("operation_id", "=", operation.id)], order="id desc", limit=1)
        return clean_for_rpc({
            "operation_id": operation.id, "state": operation.state,
            "action_id": operation.action_id,
            "reconcile_state": operation.reconcile_state,
            "readback": self._forgeflow_loads(receipt.readback_json)
            if receipt else None})

    @controlled("executor", "service")
    def forgeflow_mark_unknown(self, idempotency_key, reason=""):
        """Record that a dispatched operation's outcome is not known."""
        self.ensure_one()
        self._forgeflow_lock()
        operation = self.env["forgeflow.execution_operation"].search(
            [("idempotency_key", "=", idempotency_key), ("plan_id", "=", self.id)], limit=1)
        if not operation:
            raise UserError("operation_not_found")
        self.env.cr.execute("SELECT id FROM forgeflow_execution_operation WHERE id=%s FOR UPDATE",
                            (operation.id,))
        operation.invalidate_recordset()
        operation.write({"state": "unknown", "reconcile_state": "unknown",
                         "response_json": json.dumps({"reason": reason})})
        return {"operation_id": operation.id, "state": "unknown"}

    @controlled("executor", "service")
    def forgeflow_resolve_unknown(self, idempotency_key, confirmed_applied):
        self.ensure_one()
        self._forgeflow_lock()
        operation = self.env["forgeflow.execution_operation"].search(
            [("idempotency_key", "=", idempotency_key), ("plan_id", "=", self.id)], limit=1)
        if not operation:
            raise UserError("operation_not_found")
        self.env.cr.execute("SELECT id FROM forgeflow_execution_operation WHERE id=%s FOR UPDATE",
                            (operation.id,))
        operation.invalidate_recordset()
        operation.write({
            "state": "succeeded" if confirmed_applied else "failed",
            "reconcile_state": "confirmed_applied" if confirmed_applied
            else "confirmed_not_applied",
        })
        return {"operation_id": operation.id, "state": operation.state}

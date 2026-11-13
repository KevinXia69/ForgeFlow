from odoo import fields, models
from odoo.exceptions import AccessError

from .authorization import controlled


class ForgeFlowDecisionRecord(models.Model):
    """An approval bound to one exact plan version, environment and action set.

    A decision is never a general permission to modify business records; it names
    the plan digest it authorises.
    """

    _name = "forgeflow.decision_record"
    _inherit = "forgeflow.controlled"
    _description = "ForgeFlow approval decision"
    _order = "decided_at desc, id desc"

    plan_id = fields.Many2one("forgeflow.recovery_plan", required=True, index=True,
                              ondelete="cascade")
    company_id = fields.Many2one(related="plan_id.company_id", store=True, readonly=True)
    # Historical rows have no trustworthy technical-caller evidence. Do not
    # backfill them with the module upgrader's user; execution rejects unknown
    # provenance and every new approval records the caller explicitly.
    caller_id = fields.Many2one("res.users")
    delegation_id = fields.Many2one("forgeflow.approval_delegation")
    decision = fields.Selection(
        [("approved", "Approved"), ("rejected", "Rejected")],
        required=True, default="approved", index=True)
    approver_id = fields.Many2one("res.users", required=True, index=True)
    plan_hash = fields.Char(required=True, index=True)
    plan_version = fields.Integer()
    plan_digest = fields.Char(index=True)
    action_digest = fields.Char(index=True)
    environment_generation = fields.Char()
    execution_identity = fields.Char()
    expires_at = fields.Datetime()
    decided_at = fields.Datetime(default=fields.Datetime.now, required=True)
    comment = fields.Text()
    revoked_at = fields.Datetime()
    revoked_by_id = fields.Many2one("res.users")
    revoke_reason = fields.Text()
    active = fields.Boolean(default=True)

    @controlled("approver", "service")
    def forgeflow_revoke(self, reason=""):
        """Withdraw an approval.  It authorises nothing from this moment on."""
        self.ensure_one()
        self.plan_id._forgeflow_lock()
        self.env.cr.execute("SELECT id FROM forgeflow_decision_record WHERE id=%s FOR UPDATE",
                            (self.id,))
        self.invalidate_recordset()
        if self.env.uid != self.approver_id.id:
            if self.env.uid != self.caller_id.id or not self.delegation_id:
                raise AccessError("approval_owner_required")
            self.delegation_id._forgeflow_validate(
                self.plan_id, self.approver_id.id, self.env.uid)
        if self.revoked_at:
            return {"plan_id": self.plan_id.id, "revoked_at": str(self.revoked_at),
                    "already_revoked": True}
        if self.env["forgeflow.execution_operation"].search_count(
                [("plan_id", "=", self.plan_id.id)]):
            raise AccessError("plan_already_executing")
        self.plan_id.write({"state": "awaiting_approval"})
        self.write({
            "revoked_at": fields.Datetime.now(),
            "revoked_by_id": self.env.uid,
            "revoke_reason": str(reason or "")[:500],
        })
        return {"plan_id": self.plan_id.id, "revoked_at": str(self.revoked_at),
                "already_revoked": False}

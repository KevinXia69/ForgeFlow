"""A human grants one service permission to approve one immutable plan revision."""
from odoo import fields, models
from odoo.exceptions import AccessError, UserError

from .authorization import controlled, permission


class ForgeFlowApprovalDelegation(models.Model):
    _name = "forgeflow.approval_delegation"
    _inherit = "forgeflow.controlled"
    _description = "ForgeFlow scoped approval delegation"

    plan_id = fields.Many2one("forgeflow.recovery_plan", required=True, index=True)
    company_id = fields.Many2one(related="plan_id.company_id", store=True, readonly=True)
    approver_id = fields.Many2one("res.users", required=True)
    service_user_id = fields.Many2one("res.users", required=True)
    plan_version = fields.Integer(required=True)
    plan_digest = fields.Char(required=True)
    action_digest = fields.Char(required=True)
    expires_at = fields.Datetime(required=True)
    revoked_at = fields.Datetime()
    revoked_by_id = fields.Many2one("res.users")
    reason = fields.Text()

    def _forgeflow_validate(self, plan, approver_id, caller_id):
        self.ensure_one()
        self.check_access("read")
        self.env.cr.execute("SELECT id FROM forgeflow_approval_delegation WHERE id=%s FOR SHARE",
                            (self.id,))
        self.invalidate_recordset()
        if (self.plan_id != plan or self.company_id != plan.company_id
                or self.approver_id.id != int(approver_id)
                or self.service_user_id.id != int(caller_id)
                or self.plan_version != plan.version
                or self.plan_digest != plan._forgeflow_digest()
                or self.action_digest != plan.action_digest):
            raise UserError("approval_delegation_scope_mismatch")
        if self.revoked_at:
            raise UserError("approval_delegation_revoked")
        if not self.expires_at or self.expires_at <= fields.Datetime.now():
            raise UserError("approval_delegation_expired")
        if (not permission(self.approver_id, "approver", plan_id=plan.id)
                or not permission(self.service_user_id, "service", plan_id=plan.id)
                or plan.company_id not in self.approver_id.company_ids
                or plan.company_id not in self.service_user_id.company_ids):
            raise AccessError("approval_delegation_permission_denied")

    @controlled("approver")
    def forgeflow_revoke(self, reason=""):
        self.ensure_one()
        self.plan_id._forgeflow_lock()
        self.env.cr.execute("SELECT id FROM forgeflow_approval_delegation WHERE id=%s FOR UPDATE",
                            (self.id,))
        self.invalidate_recordset()
        if self.env.uid != self.approver_id.id:
            raise AccessError("approval_delegation_owner_required")
        if not self.revoked_at:
            self.write({"revoked_at": fields.Datetime.now(), "revoked_by_id": self.env.uid,
                        "reason": str(reason)[:500]})
        return {"delegation_id": self.id, "revoked": True}

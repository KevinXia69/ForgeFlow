from odoo import fields, models


class ForgeFlowDecisionRecord(models.Model):
    """An approval bound to one exact plan version, environment and action set.

    A decision is never a general permission to modify business records; it names
    the plan digest it authorises.
    """

    _name = "forgeflow.decision_record"
    _description = "ForgeFlow approval decision"
    _order = "decided_at desc, id desc"

    plan_id = fields.Many2one("forgeflow.recovery_plan", required=True, index=True,
                              ondelete="cascade")
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

    def forgeflow_revoke(self, reason=""):
        """Withdraw an approval.  It authorises nothing from this moment on."""
        self.ensure_one()
        if self.revoked_at:
            return {"plan_id": self.plan_id.id, "revoked_at": str(self.revoked_at),
                    "already_revoked": True}
        self.write({
            "revoked_at": fields.Datetime.now(),
            "revoked_by_id": self.env.uid,
            "revoke_reason": str(reason or "")[:500],
        })
        return {"plan_id": self.plan_id.id, "revoked_at": str(self.revoked_at),
                "already_revoked": False}

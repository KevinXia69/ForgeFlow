from odoo import fields, models


class ForgeFlowExecutionOperation(models.Model):
    """One idempotent attempt to apply one approved action."""

    _name = "forgeflow.execution_operation"
    _inherit = "forgeflow.controlled"
    _description = "ForgeFlow idempotent execution operation"
    _order = "id desc"
    _idempotency_key_unique = models.Constraint(
        "unique(idempotency_key)", "Execution idempotency key must be unique.")
    _fingerprint_unique = models.Constraint(
        "unique(fingerprint)", "An action fingerprint identifies one operation.")

    plan_id = fields.Many2one("forgeflow.recovery_plan", required=True, index=True)
    plan_version = fields.Integer()
    #: The plan-level attempt this operation belongs to.
    execution_key = fields.Char(index=True)
    #: Position in the approved action order.
    sequence = fields.Integer(default=0)
    plan_digest = fields.Char(index=True)
    action_id = fields.Char(index=True)
    operation_type = fields.Char(required=True)
    target_model = fields.Char(required=True)
    target_id = fields.Integer(required=True)
    request_hash = fields.Char(required=True, index=True)
    #: company + environment + plan version + action + params
    fingerprint = fields.Char(required=True, index=True)
    environment_generation = fields.Char()
    execution_identity = fields.Char()
    company_id = fields.Many2one(related="plan_id.company_id", store=True, readonly=True)
    idempotency_key = fields.Char(required=True, index=True)
    state = fields.Selection(
        [("not_dispatched", "Not dispatched"), ("dispatched", "Dispatched"),
         ("succeeded", "Succeeded"), ("failed", "Failed"),
         ("unknown", "Unknown"), ("reconciling", "Reconciling"),
         ("verified", "Verified")],
        default="not_dispatched", required=True, index=True)
    reconcile_state = fields.Selection(
        [("unknown", "Unknown"), ("confirmed_applied", "Confirmed applied"),
         ("confirmed_not_applied", "Confirmed not applied")])
    attempts = fields.Integer(default=0)
    request_json = fields.Text()
    response_json = fields.Text()
    dispatched_at = fields.Datetime()
    completed_at = fields.Datetime()

from odoo import fields, models


class ForgeFlowExecutionReceipt(models.Model):
    _name = "forgeflow.execution_receipt"
    _inherit = "forgeflow.controlled"
    _description = "ForgeFlow execution readback receipt"
    _order = "id desc"

    operation_id = fields.Many2one("forgeflow.execution_operation", required=True, index=True)
    company_id = fields.Many2one(related="operation_id.company_id", store=True, readonly=True)
    state = fields.Selection(
        [("pending", "Pending"), ("succeeded", "Succeeded"), ("failed", "Failed"),
         ("verified", "Verified"), ("unknown", "Unknown")],
        default="succeeded", required=True)
    readback_json = fields.Text()
    verified = fields.Boolean(default=False)
    verified_at = fields.Datetime()
    verifier_version = fields.Char()

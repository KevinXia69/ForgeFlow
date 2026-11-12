from odoo import fields, models


class ForgeFlowExceptionEvent(models.Model):
    _name = "forgeflow.exception_event"
    _description = "ForgeFlow exception event"
    _order = "occurred_at desc, id desc"

    name = fields.Char(required=True, index=True)
    event_type = fields.Char(required=True, index=True)
    partner_id = fields.Many2one("res.partner", index=True)
    product_id = fields.Many2one("product.product", index=True)
    company_id = fields.Many2one("res.company", required=True,
                                 default=lambda self: self.env.company)
    occurred_at = fields.Datetime(required=True)
    severity = fields.Selection(
        [("low", "Low"), ("medium", "Medium"), ("high", "High")],
        default="medium", required=True)
    description = fields.Text()
    #: Business state of the exception, deliberately independent from task state.
    state = fields.Selection(
        [("reported", "Reported"), ("investigating", "Investigating"),
         ("awaiting_approval", "Awaiting approval"), ("executing", "Executing"),
         ("verifying", "Verifying"), ("resolved", "Resolved"),
         ("needs_action", "Needs action"), ("cancelled", "Cancelled")],
        default="reported", required=True, index=True)
    external_event_id = fields.Char(index=True)
    plan_ids = fields.One2many("forgeflow.recovery_plan", "exception_id")

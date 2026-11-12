from odoo import fields, models


class ForgeFlowEventOutbox(models.Model):
    _name = "forgeflow.event_outbox"
    _description = "ForgeFlow transactional business event outbox"
    _order = "id asc"
    _external_event_unique = models.Constraint(
        "unique(company_id, external_event_id)",
        "External event id must be unique per company.")

    company_id = fields.Many2one("res.company", required=True, default=lambda self: self.env.company)
    event_type = fields.Char(required=True, index=True)
    aggregate_model = fields.Char(required=True)
    aggregate_id = fields.Integer(required=True)
    external_event_id = fields.Char(required=True, index=True)
    payload_json = fields.Text()
    state = fields.Selection(
        [("pending", "Pending"), ("consumed", "Consumed"), ("failed", "Failed")],
        default="pending", required=True, index=True)
    created_at = fields.Datetime(required=True, default=fields.Datetime.now)
    processed_at = fields.Datetime()

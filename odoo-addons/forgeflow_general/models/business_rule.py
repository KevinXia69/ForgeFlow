"""Formal business rules: applicability, unit, currency, validity, confirmation.

A rule extracted from a contract is a draft until a named person confirms it.
Once confirmed it is a constraint an approval depends on, so it cannot be
rewritten in place: changing what a confirmed rule means requires a new version.
The epoch is computed from rule *content* and current validity, not from a
cached hash, because a cached hash is exactly what a forgotten write path leaves
stale.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError

from . import rule_epoch


class ForgeFlowBusinessRule(models.Model):
    """A formal rule: applicability, unit, currency, validity and confirmation."""

    _name = "forgeflow.business_rule"
    _description = "ForgeFlow formal business rule"
    _order = "scope_model, name, version desc"

    name = fields.Char(required=True, index=True)
    company_id = fields.Many2one("res.company", required=True, index=True,
                                 default=lambda self: self.env.company)
    scope_model = fields.Char(required=True, index=True)
    scope_ref = fields.Char(index=True)
    condition_json = fields.Text(required=True)
    unit = fields.Char()
    currency_id = fields.Many2one("res.currency")
    valid_from = fields.Datetime()
    valid_to = fields.Datetime()
    version = fields.Integer(default=1, required=True)
    rule_hash = fields.Char(index=True)
    source_ref = fields.Char()
    confirmed_by_id = fields.Many2one("res.users")
    confirmed_at = fields.Datetime()
    state = fields.Selection(
        [("draft", "Draft"), ("confirmed", "Confirmed"), ("retired", "Retired")],
        default="draft", required=True, index=True)
    active = fields.Boolean(default=True)

    # ---------------------------------------------------------------- payload
    def _forgeflow_rule_payload(self, values=None):
        """The rule's behaviour-defining fields, optionally overlaid with writes."""
        self.ensure_one()
        values = values or {}

        def current(name):
            if name in values:
                return values[name]
            return getattr(self, name)

        currency_id = current("currency_id")
        if isinstance(currency_id, models.BaseModel):
            currency_id = currency_id.id
        currency = ""
        if currency_id:
            currency = self.env["res.currency"].browse(int(currency_id)).name or ""
        company_id = current("company_id")
        if isinstance(company_id, models.BaseModel):
            company_id = company_id.id
        return {
            "name": current("name"),
            "company": str(company_id or ""),
            "scope_model": current("scope_model"),
            "scope_ref": current("scope_ref"),
            "condition_json": current("condition_json"),
            "unit": current("unit"),
            "currency": currency,
            "valid_from": current("valid_from"),
            "valid_to": current("valid_to"),
            "version": current("version"),
            "source_ref": current("source_ref"),
        }

    # ------------------------------------------------------------------ epoch
    @api.model
    def forgeflow_rule_epoch(self):
        """Digest of every confirmed rule that is in force right now.

        A plan records this value when it is prepared.  Recomputing it before
        execution is what makes a rule change invalidate an old approval instead
        of letting it execute against rules that no longer exist.
        """
        now = fields.Datetime.now()
        rules = self.search([("state", "=", "confirmed"), ("active", "=", True)])
        rows = [rule._forgeflow_rule_payload() for rule in rules]
        return rule_epoch.epoch(rows, now=now)

    @api.model
    def forgeflow_rule_epoch_for(self, scope):
        """Digest of the confirmed rules that apply to one plan's scope.

        Only the plan's company, target models and named references count.  A
        rule for another model or another document is not a dependency, so its
        change must not block this plan.
        """
        company_id = int(scope.get("company_id") or self.env.company.id or 0)
        models = [str(item) for item in scope.get("models") or []]
        references = [str(item) for item in scope.get("refs") or []]
        domain = [("state", "=", "confirmed"), ("active", "=", True),
                  ("company_id", "=", company_id)]
        if models:
            domain.append(("scope_model", "in", models))
        if references:
            domain += ["|", ("scope_ref", "=", False),
                       ("scope_ref", "in", references)]
        else:
            domain.append(("scope_ref", "=", False))
        now = fields.Datetime.now()
        rules = self.search(domain)
        rows = [rule._forgeflow_rule_payload() for rule in rules]
        return rule_epoch.epoch(rows, now=now)

    # ------------------------------------------------------------ invariants
    def write(self, vals):
        content = rule_epoch.changed_content_fields(vals)
        if content:
            for rule in self:
                if rule.state != "confirmed":
                    continue
                # A confirmed rule is what an approval depends on.  Rewriting it
                # in place would keep the old approval meaningful against a new
                # rule body; require an explicit new version instead.
                version = int(vals.get("version", rule.version) or rule.version)
                if version <= int(rule.version):
                    raise UserError(
                        "A confirmed rule cannot be edited in place. Increase its "
                        "version to publish the change as a new revision.")
        result = super().write(vals)
        if (content or vals.get("state") == "confirmed") and "rule_hash" not in vals:
            for rule in self:
                rule.write({"rule_hash": rule_epoch.rule_content_hash(
                    rule._forgeflow_rule_payload())})
        return result
"""Server-only workflow mutations; RPC context cannot manufacture this capability."""
import logging
from functools import wraps

from odoo import api, models
from odoo.exceptions import AccessError, UserError

_TOKEN = object()
_KEY = "_forgeflow_workflow_capability"
_logger = logging.getLogger(__name__)


def permission(user, role, *, plan_id=None):
    try:
        return bool(user and user.active and user.has_group(
            f"forgeflow_general.group_forgeflow_{role}") is True)
    except Exception:
        _logger.warning("ForgeFlow permission lookup unavailable plan=%s", plan_id)
        raise UserError("approval_permission_unavailable") from None


def internal(records):
    return records.with_context(**{_KEY: _TOKEN})


def controlled(*roles):
    """Check the real RPC caller before entering an internal ORM workflow."""
    def decorate(method):
        @wraps(method)
        def call(self, *args, **kwargs):
            self.check_access("read")
            if not any(permission(self.env.user, role) for role in roles):
                raise AccessError("forgeflow_role_denied")
            companies = self.env.user.company_ids.ids
            for record in self:
                if "company_id" in record._fields and record.company_id.id not in companies:
                    raise AccessError("forgeflow_company_denied")
            return method(internal(self), *args, **kwargs)
        return call
    return decorate


class ForgeFlowControlled(models.AbstractModel):
    _name = "forgeflow.controlled"
    _description = "ForgeFlow workflow-only control records"

    def _forgeflow_require_workflow(self):
        if self.env.context.get(_KEY) is not _TOKEN:
            raise AccessError("forgeflow_workflow_required")

    @api.model_create_multi
    def create(self, vals_list):
        self._forgeflow_require_workflow()
        return super().create(vals_list)

    def write(self, vals):
        self._forgeflow_require_workflow()
        return super().write(vals)

    def unlink(self):
        raise AccessError("forgeflow_evidence_immutable")

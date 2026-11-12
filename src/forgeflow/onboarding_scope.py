"""Company and binding boundary for imports using a shared ERP connection."""
from __future__ import annotations

from typing import Any

from .onboarding_import import RELATION_FIELDS, WRITABLE_FIELDS, ImportError


class ScopedImportAdapter:
    # Shared partners/products may be referenced, but an import cannot change
    # records shared with companies outside its selected company.
    _shared = frozenset({"res.partner", "product.product"})
    _parents = {"purchase.order.line": "order_id", "sale.order.line": "order_id",
                "mrp.bom.line": "bom_id"}

    def __init__(self, adapter, *, principal_id: str, tenant_id: str,
                 odoo_user_id: int, company_id: int, admin: bool = False) -> None:
        if company_id <= 0:
            raise ImportError("import_company_scope_required")
        self.adapter = adapter
        self.company_id = company_id
        self.admin = admin
        self.access_scope = {"principal_id": principal_id, "tenant_id": tenant_id,
                             "odoo_user_id": odoo_user_id, "company_id": company_id}

    def _domain(self, model: str, *, write: bool = False) -> list:
        if model == "res.company":
            return [("id", "=", self.company_id)]
        if model == "uom.uom":
            if write and not self.admin:
                raise ImportError("import_global_model_admin_required")
            return []
        if model not in WRITABLE_FIELDS:
            raise ImportError("import_model_scope_denied")
        field = f"{self._parents[model]}.company_id" if model in self._parents else "company_id"
        if model in self._shared and not write:
            return ["|", (field, "=", False), (field, "=", self.company_id)]
        return [(field, "=", self.company_id)]

    def search_read(self, model: str, domain: list, fields: list[str],
                    limit: int = 100) -> list[dict]:
        return self.adapter.search_read(model, [*domain, *self._domain(model)], fields,
                                        limit=limit)

    def prepare_values(self, model: str, values: dict[str, Any]) -> dict[str, Any]:
        result = dict(values)
        if model != "uom.uom" and model not in self._parents:
            supplied = result.get("company_id", self.company_id)
            if supplied != self.company_id:
                raise ImportError("import_company_scope_denied")
            result["company_id"] = self.company_id
        return result

    def validate_write(self, model: str, identifier: int | None,
                       values: dict[str, Any]) -> None:
        domain = self._domain(model, write=True)
        if identifier is not None:
            rows = self.adapter.search_read(model, [("id", "=", identifier), *domain],
                                             ["id"], limit=1)
            if not rows:
                raise ImportError("import_record_scope_denied")
        if "company_id" in values and values["company_id"] != self.company_id:
            raise ImportError("import_company_scope_denied")
        for (source_model, field), (target, _) in RELATION_FIELDS.items():
            if source_model != model or field not in values:
                continue
            # The import format resolves product variants to their template ID.
            identifier = values[field]
            if not isinstance(identifier, int) or isinstance(identifier, bool):
                raise ImportError("import_relation_scope_denied")
            lookup = "product_tmpl_id" if field == "product_tmpl_id" else "id"
            if not self.search_read(target, [(lookup, "=", identifier)], ["id"], limit=1):
                raise ImportError("import_relation_scope_denied")

    def create(self, model: str, values: dict) -> int:
        values = self.prepare_values(model, values)
        self.validate_write(model, None, values)
        return self.adapter.create(model, values)

    def write(self, model: str, ids: list[int], values: dict) -> bool:
        for identifier in ids:
            self.validate_write(model, identifier, values)
        return self.adapter.write(model, ids, values)


def require_preview_scope(preview: dict, adapter) -> None:
    scope = getattr(adapter, "access_scope", None)
    if scope is not None and preview.get("access_scope") != scope:
        raise ImportError("import_access_scope_changed")

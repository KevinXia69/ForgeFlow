"""Restricted Odoo 20 read/write adapter over the verified external API.

XML-RPC is used because it is the verified Community endpoint in this deployment;
JSON-2 is an explicit future transport, not an assumed capability. The adapter
does not execute arbitrary SQL or model methods.
"""
from __future__ import annotations

import xmlrpc.client
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from .contracts import (
    ACTION_TYPES,
    ENVIRONMENT_MODELS,
    PROCUREMENT_READ_MODELS,
    REQUIRED_MODELS,
    SUPPORTED_MAJOR,
    ContractCheck,
    EnvironmentGeneration,
    OdooVersion,
)


class Odoo20Unavailable(RuntimeError):
    pass


class Odoo20Error(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class Odoo20Config:
    url: str
    database: str
    login: str
    password: str = field(repr=False)
    timeout: int = 30
    #: Company the plan must be created and executed in.  Zero keeps Odoo's
    #: default-company behaviour; a fixture or single-company deployment sets it
    #: so a plan cannot silently land in the user's default company.
    company_id: int = 0


class Odoo20Adapter:
    def __init__(self, config: Odoo20Config) -> None:
        self.config = config
        self._uid: int | None = None

    def for_company(self, company_id: int) -> Odoo20Adapter:
        """Use an explicit company for ORM defaults and company-dependent fields."""
        from dataclasses import replace

        if company_id <= 0:
            raise Odoo20Error("company_scope_required")
        scoped = Odoo20Adapter(replace(self.config, company_id=company_id))
        scoped._uid = self._uid
        return scoped

    def _company_kwargs(self) -> dict[str, Any]:
        if not self.config.company_id:
            return {}
        company = int(self.config.company_id)
        return {"context": {"allowed_company_ids": [company],
                            "default_company_id": company, "force_company": company}}

    def _common(self):
        return xmlrpc.client.ServerProxy(f"{self.config.url.rstrip('/')}/xmlrpc/2/common")

    def _models(self):
        return xmlrpc.client.ServerProxy(f"{self.config.url.rstrip('/')}/xmlrpc/2/object")

    def _authenticate(self) -> int:
        if self._uid is not None:
            return self._uid
        try:
            version = self._common().version()
            uid = self._common().authenticate(self.config.database, self.config.login,
                                               self.config.password, {})
        except Exception:
            raise Odoo20Unavailable("odoo_authentication_failed") from None
        raw = str(version.get("server_version", ""))
        if not raw.startswith(f"{SUPPORTED_MAJOR}."):
            raise Odoo20Unavailable(f"odoo_version_unsupported:{raw}")
        if not uid:
            raise Odoo20Unavailable("odoo_authentication_failed")
        self._uid = int(uid)
        return self._uid

    def version(self) -> OdooVersion:
        try:
            raw = str(self._common().version().get("server_version", ""))
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None
        try:
            major = int(raw.split(".", 1)[0])
        except ValueError:
            raise Odoo20Unavailable(f"odoo_version_unsupported:{raw}") from None
        return OdooVersion(major, raw)

    def check_contract(self) -> ContractCheck:
        try:
            version = self.version()
            if version.major != SUPPORTED_MAJOR:
                return ContractCheck(False, version, ("version",), ())
            uid = self._authenticate()
            models = self._models()
            fields_by_model = models.execute_kw(
                self.config.database, uid, self.config.password,
                "ir.model.fields", "search_read",
                [[("model", "in", list(REQUIRED_MODELS))]],
                {"fields": ["model", "name"], "limit": 10000},
            )
        except Odoo20Unavailable:
            raise
        except Exception:
            raise Odoo20Unavailable("odoo_contract_unavailable") from None
        available: dict[str, set[str]] = {}
        for row in fields_by_model:
            available.setdefault(str(row["model"]), set()).add(str(row["name"]))
        missing_models = tuple(name for name in REQUIRED_MODELS if name not in available)
        missing_fields = tuple(
            f"{model}.{field}"
            for model, required in REQUIRED_MODELS.items()
            for field in required
            if model in available and field not in available[model]
        )
        return ContractCheck(not missing_models and not missing_fields, version,
                             missing_models, missing_fields)

    def search_read(self, model: str, domain: Sequence[Any], fields: Sequence[str],
                    limit: int = 100, order: str | None = None,
                    offset: int = 0) -> list[dict[str, Any]]:
        if model in PROCUREMENT_READ_MODELS:
            allowed = set(PROCUREMENT_READ_MODELS[model])
            fields = [name for name in fields if name in allowed]
            if not fields:
                raise Odoo20Error("fields_not_allowlisted")
        elif model not in REQUIRED_MODELS:
            raise Odoo20Error("model_not_allowlisted")
        kwargs: dict[str, Any] = {"fields": list(fields), "limit": max(1, min(int(limit), 500))}
        kwargs.update(self._company_kwargs())
        if order:
            kwargs["order"] = order
        if int(offset) > 0:
            kwargs["offset"] = int(offset)
        try:
            return self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                model, "search_read", [list(domain)], kwargs)
        except xmlrpc.client.Fault:
            raise Odoo20Error("odoo_query_failed") from None
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None

    #: Fields used only to establish a bound user's company scope.  This is not a
    #: general ``res.users`` read surface: both the model and the fields are fixed
    #: here, and no caller value reaches them.
    USER_SCOPE_FIELDS = ("company_id", "company_ids")

    def user_company_ids(self, uid: int) -> tuple[int, ...]:
        try:
            rows = self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "res.users", "read", [[int(uid)]],
                {"fields": list(self.USER_SCOPE_FIELDS)})
        except xmlrpc.client.Fault:
            raise Odoo20Error("odoo_user_scope_failed") from None
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None
        if not rows:
            raise Odoo20Error("odoo_user_not_found")
        row = rows[0]
        values = list(row.get("company_ids") or [])
        company = row.get("company_id")
        if isinstance(company, (list, tuple)) and company:
            values.append(company[0])
        return tuple(sorted({int(value) for value in values
                             if isinstance(value, int) and not isinstance(value, bool)}))

    def read_bom_tree(self, product_id: int, *, max_depth: int = 8) -> dict[str, Any] | None:
        """Read a multi-level Odoo 20 BOM without mutating manufacturing data."""
        product = self.search_read("product.product", [("id", "=", int(product_id))],
                                   ["default_code", "product_tmpl_id", "uom_id"])
        if not product:
            return None
        template = product[0].get("product_tmpl_id")
        template_id = template[0] if isinstance(template, list) else template
        return self._bom_node(int(product_id), template_id, 0, max_depth, frozenset())

    def _bom_node(self, product_id: int, template_id: int | None, depth: int,
                  max_depth: int, seen: frozenset[int]) -> dict[str, Any] | None:
        if depth > max_depth or product_id in seen or not template_id:
            raise Odoo20Error("bom_cycle_or_depth")
        boms = self.search_read("mrp.bom", [("product_tmpl_id", "=", int(template_id))],
                                ["id", "product_qty", "type"], limit=1)
        if not boms:
            return None
        bom = boms[0]
        lines = self.search_read("mrp.bom.line", [("bom_id", "=", int(bom["id"]))],
                                 ["product_id", "product_qty"], limit=500)
        result = {"product_id": product_id, "bom_id": int(bom["id"]),
                  "quantity": float(bom.get("product_qty") or 1), "lines": []}
        next_seen = seen | {product_id}
        for line in lines:
            child = line.get("product_id")
            if not isinstance(child, list) or not child:
                continue
            child_id = int(child[0])
            child_product = self.search_read(
                "product.product", [("id", "=", child_id)],
                ["default_code", "product_tmpl_id"]) or [{}]
            child_template = child_product[0].get("product_tmpl_id") if child_product else None
            child_template_id = (child_template[0] if isinstance(child_template, list)
                                 else child_template)
            node = self._bom_node(child_id, child_template_id, depth + 1, max_depth, next_seen)
            result["lines"].append({
                "product_id": child_id,
                "code": child_product[0].get("default_code") if child_product else None,
                "quantity": float(line.get("product_qty") or 0),
                "bom": node,
            })
        return result

    def read_business_rules(self, scope_model: str | None = None) -> list[dict[str, Any]]:
        domain: list[Any] = [("active", "=", True)]
        if scope_model:
            domain.append(("scope_model", "=", scope_model))
        return self.search_read(
            "forgeflow.business_rule", domain,
            ["name", "scope_model", "condition_json", "version", "source_ref", "rule_hash"],
            limit=500, order="id asc")

    def create(self, model: str, values: dict[str, Any]) -> int:
        if model not in REQUIRED_MODELS:
            raise Odoo20Error("model_not_allowlisted")
        try:
            result = self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                model, "create", [values], self._company_kwargs())
        except xmlrpc.client.Fault as error:
            raise Odoo20Error(f"odoo_write_failed:{str(error)[:80]}") from None
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None
        return int(result)

    def write(self, model: str, ids: Iterable[int], values: dict[str, Any]) -> bool:
        if model not in REQUIRED_MODELS:
            raise Odoo20Error("model_not_allowlisted")
        try:
            return bool(self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                model, "write", [list(ids), values], self._company_kwargs()))
        except Exception:
            raise Odoo20Error("odoo_write_failed") from None

    def confirm_purchase_order(self, order_id: int) -> bool:
        """The only native purchase mutation exposed by this adapter."""
        try:
            return bool(self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "purchase.order", "button_confirm", [[int(order_id)]]))
        except Exception:
            raise Odoo20Error("purchase_confirmation_failed") from None

    # ------------------------------------------------- approved plan surface
    def _plan_call(self, method: str, args: list[Any], *,
                   kwargs: dict[str, Any] | None = None,
                   code: str = "plan_call_failed"):
        call_kwargs = dict(kwargs or {})
        if self.config.company_id:
            call_kwargs["context"] = {
                "allowed_company_ids": [int(self.config.company_id)],
                "default_company_id": int(self.config.company_id)}
        try:
            return self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "forgeflow.recovery_plan", method, args, call_kwargs)
        except xmlrpc.client.Fault as error:
            raise Odoo20Error(f"{code}:{str(error)[:160]}") from None
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None

    def environment_generation(self) -> EnvironmentGeneration:
        """Fingerprint the ERP environment so an approval cannot cross into another."""
        import hashlib
        import json

        version = self.version()
        try:
            modules = self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "ir.module.module", "search_read",
                [[("state", "=", "installed")]],
                {"fields": list(ENVIRONMENT_MODELS["ir.module.module"]), "limit": 2000})
        except xmlrpc.client.Fault:
            raise Odoo20Error("environment_generation_failed") from None
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None
        material = sorted((str(row.get("name")), str(row.get("latest_version") or ""))
                          for row in modules)
        digest = hashlib.sha256(json.dumps(
            {"database": self.config.database, "version": version.raw,
             "modules": material}, sort_keys=True).encode("utf-8")).hexdigest()
        return EnvironmentGeneration(digest[:32], self.config.database, version.raw,
                                     len(material))

    def prepare_plan(self, exception_id: int, actions: list[dict], fact_snapshot: dict, *,
                     environment_generation: str, rule_version: int = 1,
                     tenant_ref: str = "", name: str = "Recovery plan",
                     expires_in_hours: int = 72) -> int:
        for action in actions:
            if str(action.get("type")) not in ACTION_TYPES:
                raise Odoo20Error("action_type_not_allowed")
        return int(self._plan_call("forgeflow_prepare", [
            [], int(exception_id), actions, fact_snapshot, environment_generation],
            kwargs={"rule_version": int(rule_version), "tenant_ref": tenant_ref,
                    "name": name, "expires_in_hours": int(expires_in_hours)},
            code="plan_prepare_rejected"))

    def revoke_approval(self, plan_id: int, reason: str = "") -> dict[str, Any]:
        """Withdraw the latest approved decision and return the plan to review."""
        decisions = self.search_read(
            "forgeflow.decision_record",
            [("plan_id", "=", int(plan_id)), ("decision", "=", "approved")],
            ["id", "revoked_at"], limit=1, order="id desc")
        if not decisions:
            raise Odoo20Error("approval_not_found")
        if decisions[0].get("revoked_at"):
            return {"plan_id": int(plan_id), "already_revoked": True}
        try:
            result = self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "forgeflow.decision_record", "forgeflow_revoke",
                [[int(decisions[0]["id"])], str(reason or "")[:500]])
        except xmlrpc.client.Fault as error:
            raise Odoo20Error(f"approval_revoke_rejected:{str(error)[:120]}") from None
        self.write("forgeflow.recovery_plan", [int(plan_id)],
                   {"state": "awaiting_approval"})
        return dict(result or {})

    def approve_plan(self, plan_id: int, approver_id: int, execution_identity: str, *,
                     expires_in_hours: int = 24, comment: str = "") -> int:
        return int(self._plan_call("forgeflow_approve", [
            [int(plan_id)], int(approver_id), execution_identity],
            kwargs={"expires_in_hours": int(expires_in_hours), "comment": comment},
            code="plan_approval_rejected"))

    def apply_plan_action(self, plan_id: int, action_id: str, idempotency_key: str,
                          execution_identity: str) -> dict[str, Any]:
        return self._plan_call("forgeflow_apply_action", [
            [int(plan_id)], str(action_id), idempotency_key, execution_identity],
            code="plan_apply_rejected")

    def apply_plan(self, plan_id: int, idempotency_prefix: str,
                   execution_identity: str) -> dict[str, Any]:
        return self._plan_call("forgeflow_apply_plan", [
            [int(plan_id)], idempotency_prefix, execution_identity],
            code="plan_apply_rejected")

    def readback_plan(self, plan_id: int) -> dict[str, Any]:
        return self._plan_call("forgeflow_readback", [[int(plan_id)]],
                               code="plan_readback_rejected")

    def verify_plan(self, plan_id: int) -> dict[str, Any]:
        return self._plan_call("forgeflow_verify", [[int(plan_id)]],
                               code="plan_verify_rejected")

    def plan_checks(self, plan_id: int) -> dict[str, Any]:
        return self._plan_call("forgeflow_plan_checks", [[int(plan_id)]],
                               code="plan_checks_rejected")

    def mark_operation_unknown(self, plan_id: int, idempotency_key: str,
                               reason: str = "") -> dict[str, Any]:
        return self._plan_call("forgeflow_mark_unknown",
                               [[int(plan_id)], idempotency_key, reason],
                               code="plan_unknown_rejected")

    def resolve_unknown_operation(self, plan_id: int, idempotency_key: str,
                                  confirmed_applied: bool) -> dict[str, Any]:
        return self._plan_call("forgeflow_resolve_unknown",
                               [[int(plan_id)], idempotency_key, bool(confirmed_applied)],
                               code="plan_resolve_rejected")

    def apply_approved_plan(self, plan_id: int, idempotency_key: str,
                            operation_type: str, target_model: str,
                            target_id: int, payload: dict[str, Any]) -> dict[str, Any]:
        """Legacy entry point: the addon resolves the approved action itself."""
        try:
            return self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "forgeflow.recovery_plan", "forgeflow_apply",
                [[int(plan_id)], idempotency_key, operation_type,
                 target_model, int(target_id), payload])
        except xmlrpc.client.Fault as error:
            raise Odoo20Error(f"plan_apply_rejected:{str(error)[:120]}") from None
        except Exception:
            raise Odoo20Unavailable("odoo_unavailable") from None

    def reconcile_plan(self, plan_id: int, idempotency_key: str) -> dict[str, Any]:
        try:
            return self._models().execute_kw(
                self.config.database, self._authenticate(), self.config.password,
                "forgeflow.recovery_plan", "forgeflow_reconcile",
                [[int(plan_id)], idempotency_key])
        except Exception:
            raise Odoo20Error("reconcile_failed") from None

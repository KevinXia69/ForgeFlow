"""Run inside Odoo's shell against the installer's new local database.

All records below are explicitly synthetic. This creates ordinary ERP records,
not ForgeFlow approvals, decisions, recovery operations or execution receipts.
The entire seed and installation marker commit together, once only.
"""
from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

MARKER = "forgeflow.local_workbench.installation.v1"


class SeedError(ValueError):
    pass


def read_configuration(path: str | Path, *, expected_uid: int = 10001,
                       expected_gid: int = 10001) -> dict:
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            info = os.fstat(handle.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_size > 65536
                    or info.st_uid != expected_uid or info.st_gid != expected_gid
                    or stat.S_IMODE(info.st_mode) not in {0o400, 0o440, 0o600, 0o640}):
                raise SeedError("unsafe_odoo_bootstrap_configuration")
            value = json.load(handle)
        uuid.UUID(value["installation_id"])
        if value["schema_version"] != 1 or not value.get("database"):
            raise ValueError
        if any(not isinstance(value["operator"].get(name), str) or not value["operator"][name]
               for name in ("login", "password", "display_name")):
            raise ValueError
        if len(value.get("admin_password", "")) < 24:
            raise ValueError
        return value
    except SeedError:
        raise
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        raise SeedError("invalid_odoo_bootstrap_configuration") from None


def seed(odoo_env, config: dict) -> dict:
    if odoo_env.cr.dbname != config["database"]:
        raise SeedError("odoo_bootstrap_database_mismatch")
    odoo_env.cr.execute("SELECT pg_advisory_xact_lock(716506391)")
    parameters = odoo_env["ir.config_parameter"].sudo()
    saved = parameters.get_param(MARKER)
    if saved:
        try:
            record = json.loads(saved)
            if (record["installation_id"] != config["installation_id"]
                    or record["operator_login"] != config["operator"]["login"]
                    or record["schema_version"] != 1):
                raise ValueError
        except (ValueError, TypeError, KeyError):
            raise SeedError("odoo_installation_mismatch") from None
        # Never reset passwords, stock, order states or memberships on restart.
        return {"ok": True, "created": False, "synthetic": True,
                "company_id": record["company_id"], "operator_id": record["operator_id"]}

    users = odoo_env["res.users"].sudo()
    if users.with_context(active_test=False).search_count([("login", "=", config["operator"]["login"])]):
        raise SeedError("odoo_operator_already_exists_without_installation")
    if any(odoo_env[model].sudo().search_count([])
           for model in ("purchase.order", "sale.order", "mrp.production")):
        raise SeedError("odoo_database_not_fresh")
    module = odoo_env["ir.module.module"].sudo().search([("name", "=", "forgeflow_general")])
    if len(module) != 1 or module.state != "installed":
        raise SeedError("odoo_native_addon_not_installed")

    currency = odoo_env.ref("base.USD")
    if not currency.active:
        currency.active = True
    company = odoo_env["res.company"].sudo().create({
        "name": "ForgeFlow Local — Synthetic Company", "currency_id": currency.id})
    business = odoo_env(context={**odoo_env.context, "allowed_company_ids": [company.id],
                                "default_company_id": company.id})
    warehouse = business["stock.warehouse"].search([("company_id", "=", company.id)], limit=1)
    if not warehouse:
        warehouse = business["stock.warehouse"].create({
            "name": "Synthetic local warehouse", "code": "FFLOC", "company_id": company.id})

    group_ids = [odoo_env.ref(name).id for name in (
        "base.group_user", "sales_team.group_sale_salesman_all_leads",
        "purchase.group_purchase_user", "stock.group_stock_user", "mrp.group_mrp_user",
        "forgeflow_general.group_forgeflow_planner",
        "forgeflow_general.group_forgeflow_approver",
        "forgeflow_general.group_forgeflow_executor")]
    group_field = "group_ids" if "group_ids" in users._fields else "groups_id"
    operator = users.with_context(no_reset_password=True).create({
        "name": config["operator"]["display_name"], "login": config["operator"]["login"],
        "password": config["operator"]["password"], "company_id": company.id,
        "company_ids": [(6, 0, [company.id])], group_field: [(6, 0, group_ids)]})
    # The default database administrator must not retain the upstream password.
    odoo_env.ref("base.user_admin").sudo().write({"password": config["admin_password"]})

    def partner(name, **values):
        return business["res.partner"].create({"name": "Synthetic " + name,
                                               "company_id": company.id, **values})

    vendor = partner("primary supplier", supplier_rank=1)
    alternative = partner("alternative supplier", supplier_rank=1)
    customer = partner("customer", customer_rank=1)
    unit = odoo_env.ref("uom.product_uom_unit")

    def product(code, name, cost):
        values = {"name": "Synthetic " + name, "default_code": code, "type": "consu",
                  "uom_id": unit.id, "purchase_ok": True, "sale_ok": True,
                  "standard_price": cost, "list_price": cost * 2, "company_id": company.id}
        if "is_storable" in business["product.product"]._fields:
            values["is_storable"] = True
        return business["product.product"].create(values)

    finished = product("FF-LOCAL-ASSEMBLY", "control assembly (training only)", 10)
    component = product("FF-LOCAL-COMPONENT", "assembly component (training only)", 3)
    for supplier, price, delay in ((vendor, 10, 3), (alternative, 12, 2)):
        business["product.supplierinfo"].create({
            "partner_id": supplier.id, "product_tmpl_id": finished.product_tmpl_id.id,
            "product_id": finished.id, "min_qty": 1, "price": price,
            "delay": delay, "company_id": company.id, "currency_id": currency.id})

    bom = business["mrp.bom"].create({"product_tmpl_id": finished.product_tmpl_id.id,
                                      "product_id": finished.id, "product_qty": 1,
                                      "type": "normal", "company_id": company.id})
    line_model = business["mrp.bom.line"]
    unit_field = "uom_id" if "uom_id" in line_model._fields else "product_uom_id"
    line_model.create({"bom_id": bom.id, "product_id": component.id,
                       "product_qty": 1, unit_field: unit.id})
    workcenter = business["mrp.workcenter"].create({
        "name": "Synthetic assembly station", "code": "FF-LOCAL-WC", "company_id": company.id})
    business["mrp.routing.workcenter"].create({
        "name": "Synthetic assembly", "bom_id": bom.id, "workcenter_id": workcenter.id,
        "time_mode": "manual", "time_cycle_manual": 5})
    business["stock.quant"]._update_available_quantity(finished, warehouse.lot_stock_id, 3)
    business["stock.quant"]._update_available_quantity(component, warehouse.lot_stock_id, 20)

    due = datetime.now(UTC).replace(tzinfo=None, microsecond=0) + timedelta(days=21)
    purchase = business["purchase.order"].create({
        "name": "FF-LOCAL-PO-001", "partner_id": vendor.id, "company_id": company.id,
        "currency_id": currency.id, "picking_type_id": warehouse.in_type_id.id,
        "origin": "Synthetic local installation; no real supplier order"})
    purchase_line = business["purchase.order.line"]
    unit_field = "product_uom_id" if "product_uom_id" in purchase_line._fields else "product_uom"
    purchase_line.create({"order_id": purchase.id, "product_id": finished.id,
                          "name": "Synthetic assembly supply", "product_qty": 12,
                          "price_unit": 10, "date_planned": due, unit_field: unit.id})
    # A cancelled synthetic supply is inspectable by the real replacement
    # planner. Polling starts disabled; no model call or recovery is dispatched.
    purchase.button_confirm()
    purchase.button_cancel()
    sale = business["sale.order"].create({
        "name": "FF-LOCAL-SO-001", "partner_id": customer.id, "company_id": company.id,
        "commitment_date": due, "note": "Synthetic training demand; no real customer order"})
    sale_line = business["sale.order.line"]
    values = {"order_id": sale.id, "product_id": finished.id,
              "name": "Synthetic assembly demand", "product_uom_qty": 12, "price_unit": 20}
    if "product_uom_id" in sale_line._fields:
        values["product_uom_id"] = unit.id
    elif "product_uom" in sale_line._fields:
        values["product_uom"] = unit.id
    sale_line.create(values)
    # A draft sales order is an explicit planned demand, not a fabricated direct
    # stock-move relation. Users can confirm/edit it through their local Odoo.
    record = {"schema_version": 1, "installation_id": config["installation_id"],
              "operator_login": config["operator"]["login"], "operator_id": operator.id,
              "company_id": company.id, "purchase_id": purchase.id, "sale_id": sale.id,
              "product_ids": [finished.id, component.id], "synthetic": True}
    parameters.set_param(MARKER, json.dumps(record, sort_keys=True))
    return {"ok": True, "created": True, "synthetic": True,
            "company_id": company.id, "operator_id": operator.id,
            "purchase_orders": 1, "sales_orders": 1, "products": 2,
            "recovery_approvals": 0, "recovery_executions": 0}


_SEED_ATTRIBUTES = frozenset(seed.__code__.co_names)


def safe_error_details(error: Exception) -> dict:
    """Expose code locations only; exception messages and locals may be secret."""
    detail = {}
    owned_code = {seed.__code__, read_configuration.__code__, run.__code__}
    frame = error.__traceback__
    while frame is not None:
        if frame.tb_frame.f_code in owned_code:
            detail["error_line"] = frame.tb_lineno
        frame = frame.tb_next
    if isinstance(error, AttributeError) and error.name in _SEED_ATTRIBUTES:
        detail["error_attribute"] = error.name
    return detail


def run(odoo_env) -> None:
    try:
        configuration = read_configuration(os.environ.get(
            "FORGEFLOW_ODOO_BOOTSTRAP_CONFIG", "/etc/forgeflow/bootstrap.json"))
        result = seed(odoo_env, configuration)
        odoo_env.cr.commit()
    except Exception as error:
        odoo_env.cr.rollback()
        code = str(error) if isinstance(error, SeedError) else type(error).__name__
        print(json.dumps({"ok": False, "component": "odoo_seed", "error": code,
                          **safe_error_details(error)}), flush=True)
        raise SystemExit(1) from None
    print(json.dumps(result), flush=True)


if "env" in globals():
    run(globals()["env"])

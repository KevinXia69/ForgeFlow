"""Run through `odoo shell` only in a disposable Compose integration project.

The preceding odoo-init must already have performed the first real seed. This
probe verifies native records and that repeat bootstrap preserves every row in
the affected business/auth tables. It does not approve or execute recovery.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import uuid


def probe(odoo_env):
    if os.environ.get("FORGEFLOW_WORKBENCH_ISOLATED_TEST") != "1":
        raise RuntimeError("disposable_workbench_probe_required")
    if odoo_env.cr.dbname != "forgeflow_local":
        raise RuntimeError("unexpected_probe_database")
    spec = importlib.util.spec_from_file_location("workbench_seed", "/opt/forgeflow/odoo_bootstrap.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    configuration = module.read_configuration("/etc/forgeflow/bootstrap.json")
    record = json.loads(odoo_env["ir.config_parameter"].sudo().get_str(module.MARKER))
    assert record["installation_id"] == configuration["installation_id"]
    assert record["synthetic"] is True
    operator = odoo_env["res.users"].browse(record["operator_id"])
    assert operator.exists() and operator.active and operator.id != 1
    assert operator.company_ids.ids == [record["company_id"]]
    assert operator.login == configuration["operator"]["login"]
    for role in ("planner", "approver", "executor"):
        assert operator.has_group("forgeflow_general.group_forgeflow_" + role)
    assert not operator.has_group("base.group_system")
    purchase = odoo_env["purchase.order"].browse(record["purchase_id"])
    sale = odoo_env["sale.order"].browse(record["sale_id"])
    assert purchase.exists() and purchase.state == "cancel"
    assert sale.exists() and sale.state == "draft"
    assert purchase.company_id.id == sale.company_id.id == record["company_id"]
    assert len(purchase.order_line) == len(sale.order_line) == 1
    assert purchase.order_line.product_id.id == sale.order_line.product_id.id
    product = purchase.order_line.product_id
    assert purchase.order_line.product_qty == sale.order_line.product_uom_qty == 12
    assert purchase.order_line.uom_id == product.uom_id == sale.order_line.product_uom_id
    assert product.id in record["product_ids"]
    assert odoo_env["mrp.bom"].search_count([("product_id", "=", product.id)]) == 1
    assert odoo_env["product.supplierinfo"].search_count([("product_id", "=", product.id)]) == 2
    for model in ("forgeflow.recovery_plan", "forgeflow.decision_record",
                  "forgeflow.execution_operation", "forgeflow.execution_receipt"):
        assert odoo_env[model].search_count([("company_id", "=", record["company_id"])]) == 0

    tables = ("res_company", "res_users", "res_partner", "product_template", "product_product",
              "product_supplierinfo", "stock_warehouse", "stock_location", "stock_quant",
              "stock_move", "stock_move_line", "stock_picking", "purchase_order", "purchase_order_line",
              "sale_order", "sale_order_line", "mrp_bom", "mrp_bom_line", "mrp_workcenter",
              "mrp_routing_workcenter", "ir_config_parameter")

    def snapshot():
        result = {}
        for table in tables:
            # Names come from this closed tuple, never from configuration/input.
            odoo_env.cr.execute('SELECT count(*), md5(coalesce(string_agg(row_to_json(t)::text, '
                                "'|' ORDER BY t.id), '')) FROM " + table + " t")
            result[table] = odoo_env.cr.fetchone()
        return result

    before = snapshot()
    assert module.seed(odoo_env, configuration)["created"] is False
    assert module.seed(odoo_env, configuration)["created"] is False
    assert snapshot() == before
    foreign = {**configuration, "installation_id": str(uuid.uuid4())}
    try:
        module.seed(odoo_env, foreign)
    except module.SeedError as error:
        assert str(error) == "odoo_installation_mismatch"
    else:
        raise AssertionError("foreign installation accepted")
    assert snapshot() == before
    odoo_env.cr.rollback()
    return {"ok": True, "real_odoo": True, "repeat_seed_preserved_tables": len(tables),
            "business_snapshot_sha256": hashlib.sha256(
                json.dumps(before, sort_keys=True).encode()).hexdigest(),
            "foreign_installation_rejected": True, "synthetic_roles_and_business_verified": True,
            "recovery_approvals_created": 0, "recovery_executions_created": 0}


if "env" in globals():
    print("FORGEFLOW_WORKBENCH_SEED_PROBE=" + json.dumps(probe(globals()["env"])), flush=True)

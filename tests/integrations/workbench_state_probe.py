"""Read-only native state digest, restricted to the disposable acceptance stack."""
from __future__ import annotations

import json
import os

TABLES = (
    "res_company", "product_template", "product_product", "product_supplierinfo",
    "stock_warehouse", "stock_location", "stock_quant", "stock_move", "stock_move_line",
    "stock_picking", "purchase_order", "purchase_order_line", "sale_order", "sale_order_line",
    "mrp_bom", "mrp_bom_line", "mrp_workcenter", "mrp_routing_workcenter",
    "forgeflow_exception_event", "forgeflow_recovery_plan", "forgeflow_decision_record",
    "forgeflow_execution_operation", "forgeflow_execution_receipt", "forgeflow_approval_delegation",
)


def snapshot(odoo_env):
    if (os.environ.get("FORGEFLOW_WORKBENCH_ISOLATED_TEST") != "1"
            or odoo_env.cr.dbname != "forgeflow_local"):
        raise RuntimeError("disposable_workbench_probe_required")
    rows = {}
    for table in TABLES:
        # Closed identifiers; only counts and one-way digests leave the database.
        odoo_env.cr.execute("SELECT count(*), md5(coalesce(string_agg(row_to_json(t)::text, "
                            "'|' ORDER BY t.id), '')) FROM " + table + " t")
        count, digest = odoo_env.cr.fetchone()
        rows[table] = {"count": count, "digest": digest}
    odoo_env.cr.rollback()
    return {"ok": True, "read_only": True, "tables": rows}


if "env" in globals():
    print("FORGEFLOW_WORKBENCH_STATE_PROBE=" + json.dumps(snapshot(globals()["env"])), flush=True)

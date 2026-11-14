"""Read-only durable identity/catalog snapshots for disposable stack acceptance."""
from __future__ import annotations

import json
import os

TABLES = (
    "forgeflow_local_installation", "control_tenants", "control_principals",
    "control_odoo_bindings", "control_source_subscriptions", "knowledge_documents",
    "knowledge_document_versions", "knowledge_document_blocks", "knowledge_generations",
    "knowledge_role_scopes", "knowledge_publication_counters",
)


def snapshot():
    if os.environ.get("FORGEFLOW_WORKBENCH_ISOLATED_TEST") != "1":
        raise RuntimeError("disposable_workbench_probe_required")
    import psycopg
    from psycopg import sql

    from forgeflow.local_workbench.config import load_config

    config = load_config()
    tables = {}
    with psycopg.connect(config["env"]["FORGEFLOW_CONTROL_DATABASE_URL"]) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        for table in TABLES:
            row = connection.execute(sql.SQL(
                "SELECT count(*),md5(coalesce(string_agg(row_to_json(t)::text,"
                "'|' ORDER BY row_to_json(t)::text),'')) FROM {} t").format(
                    sql.Identifier(table))).fetchone()
            tables[table] = {"count": row[0], "digest": row[1]}
    return {"ok": True, "read_only": True, "tables": tables}


if __name__ == "__main__":
    try:
        result = snapshot()
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__}
    print("FORGEFLOW_WORKBENCH_CONTROL_PROBE=" + json.dumps(result), flush=True)
    raise SystemExit(0 if result["ok"] else 1)

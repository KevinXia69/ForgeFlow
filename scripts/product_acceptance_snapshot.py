#!/usr/bin/env python3
"""Record read-only business-table digests for an explicitly named ERP test sandbox.

Uses the existing launcher's Docker configuration. No ERP credential or business
row contents are printed or saved. This is product integration evidence, not an
agent evaluation or a substitute for the task's original verifier.
"""
from __future__ import annotations

import argparse
import json
import re
import runpy
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TABLES = (
    "purchase_order", "purchase_order_line", "mrp_production", "mrp_workorder",
    "sale_order", "sale_order_line", "stock_move", "stock_picking",
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--container", required=True)
    parser.add_argument("--label", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}", args.container):
        parser.error("invalid sandbox container name")
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,79}", args.label):
        parser.error("invalid evidence label")
    if args.out.exists():
        parser.error("evidence already exists; use a new output path")
    launcher = runpy.run_path(str(ROOT / "scripts/local_workbench.py"), run_name="acceptance")
    tables = {}
    for table in TABLES:
        sql = (
            "SELECT count(*) || ':' || coalesce(md5(string_agg("
            "row_to_json(t)::text,',' ORDER BY id)),'empty') FROM " + table + " t;"
        )
        result = launcher["_docker"](
            "exec", "--user", "postgres", args.container, "psql", "-d", "bench", "-At", "-c", sql,
            capture=True,
        )
        count, digest = result.stdout.strip().split(":")
        tables[table] = {"count": int(count), "digest": digest}
    payload = {
        "scope": "Dedicated synthetic ERP-Bench product acceptance; not autonomous evaluation",
        "label": args.label, "recorded_at": datetime.now(UTC).isoformat(),
        "container": args.container, "tables": tables,
    }
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(payload, indent=2) + "\n")
    print(json.dumps({"label": args.label, "table_counts": {
        name: value["count"] for name, value in tables.items()
    }}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

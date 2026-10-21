"""Package an allowlist of generic solver sources, never task artifacts."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

ERP_BENCH_COMMIT = "ceba3880af555129b5278e056a0c20f2fb5a0ba9"
UPSTREAM_FILES = {
    "erp_bench/procurement/solver.py": "upstream/procurement/solver.py",
    "erp_bench/procurement/procurement_objectives.py":
        "upstream/procurement/procurement_objectives.py",
    "schemas/models.py": "upstream/schemas/models.py",
    "schemas/procurement_invoicing_validation.py":
        "upstream/schemas/procurement_invoicing_validation.py",
    "LICENSE": "upstream/LICENSE",
}
OWN_FILES = ("__init__.py", "live.py", "tool.py", "objectives.py")
PLANNER_ROOT = "/opt/forgeflow/planner"
PLANNER_PYTHON = "/opt/forgeflow/planner-env/bin/python"
PLANNER_ENTRY = f"{PLANNER_ROOT}/plan.py"
PLANNER_HELP = """A read-only CP-SAT planning tool is available.
Read /opt/forgeflow/planner/USAGE.md, collect request facts from the task and live
Odoo, and run /opt/forgeflow/planner-env/bin/python /opt/forgeflow/planner/plan.py
with --request and --output. It queries live Odoo and computes a new plan; it
does not execute changes or validate the final ERP state. Preserve its initial
snapshot before making writes. Missing/ambiguous business inputs are errors.
The generated plan references must be mapped to real Odoo record names and
dates. Retained and changed POs/MOs must have valid active consumer origins.
After execution, re-read dates, quantities, status, workcenters and lineage.
"""
USAGE = """# ForgeFlow read-only planner

This tool copies ERP-Bench's generic CP-SAT solver, not any task-specific plan,
scenario, seed record or verifier. It uses only the request and live Odoo API.
It supports one target product, recursively collected BoMs, integer units,
USD pricing and unexecuted commitments. Unsupported or missing facts fail.

Create a request JSON from the task and Odoo:

```json
{
  "target_product_code": "actual SKU from Odoo",
  "sales_order_ids": [1, 2],
  "anchor_date": "YYYY-MM-DD",
  "margin": 0.275,
  "sales_order_budget_caps": {"1": 10000, "2": 12000},
  "disruption": {"kind": "supplier_cancellation", "vendor_ref": "actual vendor ref"}
}
```

For an outage, use kind `workcenter_outage` and the actual `workcenter_code`.
Dates and amounts above are illustrative; use the actual task facts.
If a vendor has multiple relevant offers, specify an unambiguous `offer_key`.
The anchor is the task's planning day. Do not infer it from a modified PO date.

Run before any writes:

```
/opt/forgeflow/planner-env/bin/python /opt/forgeflow/planner/plan.py \
  --request /workspace/request.json --output /workspace/planning
```

The output directory must not exist. It preserves request.json, snapshot.json,
blueprint.json and plan.json. A plan contains quantity, source, cost, customer
allocation, relative dates and abstract plan references. None is an Odoo ID.
The tool never writes Odoo. A successful solve does not mean execution passed.

Map references to retained or new real records. Relative day dates use the
request anchor. Keep feasible existing commitments and their IDs. Cancel the
broken path, preserve unaffected work, and update dependent origins when an MO
is canceled or reassigned. A component PO must reference its active consumer
MOs; a finished PO or MO must reference the actual sales orders. Re-read the
complete resulting graph and check the business requirements after executing.

The planning-metadata-v1 profile explicitly exposes fixed horizon standard
unit times/costs in workcenter notes. This model is not a machine calendar
schedule and does not resolve calendar-duration discrepancies by itself.
"""


def build_planner_bundle(root: Path, destination: Path, *, recovery: bool = False) -> dict:
    source = root / "vendor/erp-bench"
    provenance = json.loads((root / "vendor/erp-bench.source.json").read_text())
    if provenance.get("commit") != ERP_BENCH_COMMIT:
        raise ValueError("The planner requires the pinned ERP-Bench source.")
    contents = {}
    for relative, target in UPSTREAM_FILES.items():
        path = source / relative
        if path.is_symlink() or not path.is_file():
            raise ValueError("Planner source must be a regular file.")
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != provenance["files"][relative]["sha256"]:
            raise ValueError("Planner generic source differs from its pinned digest.")
        contents[target] = data
    for name in OWN_FILES:
        path = root / "src/forgeflow/planning" / name
        if path.is_symlink() or not path.is_file():
            raise ValueError("The live planner implementation is incomplete.")
        contents[f"forgeflow/planning/{name}"] = path.read_bytes()
    for name in ("forgeflow/__init__.py", "upstream/procurement/__init__.py",
                 "upstream/schemas/__init__.py"):
        contents[name] = b""
    credentials = root / "src/forgeflow/odoo_credentials.py"
    if credentials.is_symlink() or not credentials.is_file():
        raise ValueError("The planner credentials helper is missing.")
    contents["forgeflow/odoo_credentials.py"] = credentials.read_bytes()
    contents["plan.py"] = (
        "from forgeflow.planning.tool import main\n"
        "raise SystemExit(main())\n"
    ).encode()
    contents["USAGE.md"] = USAGE.encode()
    contents["requirements.txt"] = (root / "build-inputs/planner/requirements.txt").read_bytes()
    if recovery:
        for name in ("__init__", "change_set", "compiler", "readback", "scope_guard",
                     "journal", "odoo", "storage", "service", "product_versions", "product_constraints", "product_costs",
                     "product_candidates", "failure"):
            path = root / "src/forgeflow/recovery" / (name + ".py")
            if path.is_symlink() or not path.is_file():
                raise ValueError("Recovery source must be a regular file.")
            contents[f"forgeflow/recovery/{name}.py"] = path.read_bytes()
        for name in ("approval", "contract"):
            path = root / "src/forgeflow" / (name + ".py")
            if path.is_symlink() or not path.is_file():
                raise ValueError("Recovery support module must be a regular file.")
            contents[f"forgeflow/{name}.py"] = path.read_bytes()
        for name in ("client", "index"):
            path = root / "src/forgeflow/recovery" / (name + ".py")
            if path.is_symlink() or not path.is_file():
                raise ValueError("Recovery client module must be a regular file.")
            contents[f"forgeflow/recovery/{name}.py"] = path.read_bytes()
        for name in ("__init__", "bootstrap", "daemon", "handlers", "main", "peer", "protocol"):
            path = root / "src/forgeflow/executor" / (name + ".py")
            if path.is_symlink() or not path.is_file():
                raise ValueError("Executor module must be a regular file.")
            contents[f"forgeflow/executor/{name}.py"] = path.read_bytes()
        contents["recover.py"] = (
            "from forgeflow.recovery.service import main\n"
            "raise SystemExit(main())\n"
        ).encode()
        contents["RECOVERY.md"] = (
            "# ForgeFlow planned-document recovery\n\n"
            "Use prepare_recovery with explicit task facts, then execute_recovery with its plan_id.\n"
            "Preparation reads current Odoo and freshly solves; execution applies only the bound\n"
            "keep/update/cancel/create change set, journals and reads back actual documents.\n"
            "Do not treat target totals as additional supply. Do not fabricate receipts or\n"
            "manufacturing completion. A stopped execution may have committed changes: preserve\n"
            "its plan_id and journal. Never recapture its baseline or create replacement documents\n"
            "blindly. The external ERP-Bench verifier alone determines benchmark success.\n"
            "Local journals detect corruption and concurrent tool calls; they do not isolate\n"
            "against another process with the same user permissions or other ERP writers.\n"
        ).encode()
    record = {
        "profile": "cp-sat-live-v1", "upstream_commit": ERP_BENCH_COMMIT,
        "task_specific_files": False, "odoo_writes": False,
        "files_sha256": {name: hashlib.sha256(data).hexdigest()
                         for name, data in sorted(contents.items())},
    }
    if recovery:
        record.update(profile="cp-sat-recovery-v1", recovery_enabled=True, odoo_writes=True)
        from .objectives import PRODUCT_PROFILES

        record["product_candidate_profiles"] = list(PRODUCT_PROFILES)
    contents["provenance.json"] = (json.dumps(record, indent=2) + "\n").encode()
    with tarfile.open(destination, "w:gz") as archive:
        for name, data in sorted(contents.items()):
            info = tarfile.TarInfo("planner/" + name)
            info.size = len(data)
            info.mode = 0o444
            archive.addfile(info, io.BytesIO(data))
    return record

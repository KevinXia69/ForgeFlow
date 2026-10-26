#!/usr/bin/env python3
"""Prepare/build a local A/B workbench image without starting a sandbox."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def prepare(scenario: str) -> tuple[Path, str]:
    from forgeflow.local_entrypoint import preserving_source
    from forgeflow.runner import stage_task

    builds = ROOT / ".runtime" / "local-image-builds"
    builds.mkdir(parents=True, exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix=f"{scenario.lower()}-", dir=builds))
    task, derivation = stage_task(
        ROOT, scenario, stage, agent="oracle", data_profile="planning-metadata-v1",
    )
    context = task / "environment"
    # Refuse any upstream entrypoint outside the preserving launcher's contract.
    _, entrypoint = preserving_source((context / "entrypoint.sh").read_bytes())
    shutil.copyfile(ROOT / "deploy/local/Dockerfile.odoo", context / "Dockerfile")
    shutil.copyfile(ROOT / "deploy/local/setup-bootstrap.py", context / "setup-bootstrap.py")
    shutil.copyfile(ROOT / "deploy/vps/odoo-uv.py", context / "odoo-uv.py")
    files = {path.name: hashlib.sha256(path.read_bytes()).hexdigest()
             for path in sorted(context.iterdir()) if path.is_file()}
    (stage / "build.json").write_text(json.dumps({
        "scenario": scenario, "task_derivation": derivation,
        "entrypoint_contract": entrypoint, "build_context_sha256": files,
    }, indent=2) + "\n", encoding="utf-8")
    return context, f"forgeflow-planner-live:{scenario.lower()}-20261020"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=("A", "B"), default="A")
    parser.add_argument("--tag", help="Override the local workbench's default image tag")
    parser.add_argument("--prepare-only", action="store_true", help="Stage inputs without Docker")
    args = parser.parse_args()
    if os.name != "posix":
        parser.error("Use Linux or WSL2; upstream verification preserves Unix file permissions.")
    subprocess.run([sys.executable, str(ROOT / "scripts/fetch_upstream.py"),
                    "erp-bench", "--verify-only"], check=True, cwd=ROOT)
    context, default_tag = prepare(args.scenario)
    tag = args.tag or default_tag
    print(f"Verified build context: {context}", flush=True)
    print(f"Image tag: {tag}", flush=True)
    if not args.prepare_only:
        subprocess.run(["docker", "build", "--tag", tag, str(context)], check=True, cwd=ROOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

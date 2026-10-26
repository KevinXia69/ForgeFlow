#!/usr/bin/env python3
"""Build the same visible planning-note variant as ForgeFlow's local runner."""
from __future__ import annotations

import hashlib
import inspect
import json
import sys
from pathlib import Path


def derive(source: Path, destination: Path, evidence: Path) -> dict:
    from forgeflow.runner import _planning_metadata_projection

    original = source.read_bytes()
    derived, record, diff = _planning_metadata_projection(original.decode("utf-8"))
    if record.get("preserved_non_note_fields_verified") is not True:
        raise ValueError("planning_projection_not_verified")
    runner = Path(inspect.getsourcefile(_planning_metadata_projection))
    record.update(
        schema=1, task="2280_medium_repair_plan_medium",
        upstream_commit="ceba3880af555129b5278e056a0c20f2fb5a0ba9",
        runner_sha256=hashlib.sha256(runner.read_bytes()).hexdigest(),
    )
    evidence.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(derived.encode("utf-8"))
    (evidence / "scenario_data.json").write_bytes(derived.encode("utf-8"))
    (evidence / "planning-metadata-v1.diff").write_bytes(diff.encode("utf-8"))
    (evidence / "derivation.json").write_text(json.dumps(record, sort_keys=True, indent=2) + "\n")
    if source.read_bytes() != original:
        raise ValueError("original_scenario_was_modified")
    return record


if __name__ == "__main__":
    sys.path.insert(0, "/opt/forgeflow/derivation-code")
    result = derive(Path("/opt/forgeflow/upstream/scenario_data.json"),
                    Path("/setup/scenario_data.json"), Path("/opt/forgeflow/task-derivation"))
    print(json.dumps({"profile": result["name"], "source_sha256": result["source_sha256"],
                      "execution_sha256": result["execution_sha256"],
                      "changed_fields": result["changed_fields"]}, sort_keys=True))

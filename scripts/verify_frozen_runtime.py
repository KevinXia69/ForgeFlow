"""Offline acceptance of the actual pinned Pi/Harbor import and config path.

Uses project staging helpers only. It never starts Docker, dispatches a model,
prepares a business plan or applies ERP changes. Output contains measured paths
and digests, not configuration credentials or task reference contents.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path

from forgeflow.config import load_config
from forgeflow.frozen_execution import materialize_execution, verify_execution
from forgeflow.host_runtime import PATH_BOOTSTRAP, ensure_host_runtime, verify_host_runtime
from forgeflow.runner import build_run_command, capture_implementation_snapshot, stage_task
from forgeflow.runtime_lifecycle import exclusive_lease

IMPORTS = """
import importlib, json, pathlib, sys
source = pathlib.Path(sys.argv[1]).resolve()
paths = {}
for name in ('forgeflow.runner', 'forgeflow.agents.pi_nebius', 'forgeflow.verifier',
             'forgeflow.budget', 'forgeflow.environment', 'forgeflow.research_containers'):
    module = importlib.import_module(name)
    origin = pathlib.Path(module.__file__).resolve()
    assert origin.is_relative_to(source / 'src')
    paths[name] = origin.relative_to(source).as_posix()
from forgeflow.agents.pi_nebius import WORKSPACE_ROOT, UpstreamPi
assert WORKSPACE_ROOT == source
assert UpstreamPi.__module__ == '_forgeflow_upstream_pi'
print(json.dumps({'imports': paths, 'pinned_upstream_adapter_loaded': True}))
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    report = {"scope": "offline_actual_pinned_imports_and_harbor_config",
              "model_called": False, "docker_started": False, "erp_written": False}
    env = {k: os.environ[k] for k in ("PATH", "HOME", "TMPDIR", "LANG") if k in os.environ}
    env.update(PYTHONDONTWRITEBYTECODE="1", PYTHONNOUSERSITE="1")
    try:
        config = load_config(root)
        with exclusive_lease(root / ".runtime/lifecycle/heavy-operation.lock"):
            run = Path(tempfile.mkdtemp(prefix="forgeflow-frozen-imports-")).resolve()
            snapshot = capture_implementation_snapshot(root, run, secrets=(config.api_key,))
            frozen = materialize_execution(root, run, snapshot, agent="pi",
                                           planner_profile="cp-sat-recovery-v1",
                                           secrets=(config.api_key,))
            source = run / frozen["root"]
            runtime = ensure_host_runtime(source, root / ".runtime/frozen-host", root / ".venv/bin/python")
            check = subprocess.run(
                [runtime["python"], "-I", "-B", "-S", "-c", PATH_BOOTSTRAP + IMPORTS,
                 str(source / "src"), runtime["packages"], str(source)],
                cwd=source, env=env, capture_output=True, text=True, timeout=60, check=False,
            )
            if check.returncode:
                raise RuntimeError("Actual frozen Pi import failed.")
            report.update(json.loads(check.stdout))
            task, _ = stage_task(root, "2280_medium_repair_plan_medium", run, agent="pi",
                                  allowed_host="api.tokenfactory.nebius.com")
            command = build_run_command(root, "A", "pi", run, model="nvidia/offline-config-only",
                                         agent_timeout_sec=600, task_path=task,
                                         planner_profile="cp-sat-recovery-v1", execution_root=source,
                                         host_runtime=runtime)
            result = subprocess.run(command + ["--print-config"], cwd=source, env=env,
                                    capture_output=True, text=True, timeout=60, check=False)
            if result.returncode:
                raise RuntimeError("Actual frozen Harbor configuration failed.")
            resolved = json.loads(result.stdout)
            selected_environment = resolved.get("environment", {}).get("import_path")
            if selected_environment != "forgeflow.environment:GuardedDockerEnvironment":
                raise RuntimeError("Guarded environment was not selected in the actual Harbor configuration.")
            report.update(status="passed", source_integrity=verify_execution(run, frozen),
                           runtime_integrity=verify_host_runtime(runtime),
                           source_index_sha256=frozen["index_sha256"],
                           python_dependencies_frozen=True,
                           configuration_parsed=isinstance(resolved, dict),
                           environment_import_path=selected_environment,
                           temporary_evidence_root=str(run))
    except Exception as exc:
        report.update(status="failed", error_type=type(exc).__name__)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())

"""Real Docker/Harbor timeout and paused-container cleanup, without ERP or models.

Uses a local existing image with its entrypoint replaced by a sleeping Python
process. Retains the stopped test containers; never starts an ERP entrypoint.
The operator starts/stops the VM separately. No images are pulled.
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path

from forgeflow.research_containers import (
    _command,
    _containers,
    _inspect,
    docker_environment,
    quiesce_run,
    wait_host_group,
)
from forgeflow.run_watchdog import RunWatchdog
from forgeflow.runner import _child_environment, _stop_process_group
from forgeflow.runtime_lifecycle import exclusive_lease, spawn_managed

ROOT = Path(__file__).resolve().parents[1]


def need(condition, code):
    if not condition:
        raise RuntimeError(code)


async def child(run_dir: Path, image: str, project: str):
    from harbor.models.task.config import EnvironmentConfig
    from harbor.models.trial.paths import TrialPaths

    from forgeflow.environment import GuardedDockerEnvironment

    directory = run_dir / "task/quiescence/environment"
    directory.mkdir(parents=True)
    (directory / "docker-compose.yaml").write_text(json.dumps({"services": {"main": {
        "entrypoint": ["python3", "-c", "import time; time.sleep(600)"],
        "command": [], "network_mode": "none",
    }}}))
    trial = run_dir / "harbor/job/trial"
    trial.mkdir(parents=True)
    environment = GuardedDockerEnvironment(
        environment_dir=directory, environment_name="quiescence", session_id=project,
        trial_paths=TrialPaths(trial_dir=trial),
        task_env_config=EnvironmentConfig(docker_image=image, cpus=1, memory_mb=512),
    )
    await environment.start(force_build=False)
    (run_dir / "ready").write_text("ready\n")
    # Simulates a blocked host worker. The independent supervisor must stop it.
    await asyncio.sleep(600)


def acceptance(output: Path, image: str) -> int:
    need(not output.exists(), "evidence_already_exists")
    output.mkdir(parents=True)
    environment = docker_environment(_child_environment(ROOT))
    image_id = _command(["image", "inspect", "--format", "{{.Id}}", image], environment)
    need(image_id.startswith("sha256:"), "existing_local_image_required")
    run_dir = Path(tempfile.mkdtemp(prefix="forgeflow-quiescence-")).resolve()
    project = "ff-quiescence-" + uuid.uuid4().hex[:12]
    report = {"scope": "controlled_real_docker_timeout_and_pause_no_erp_no_model",
              "status": "running", "image_id": image_id, "project": project,
              "erp_started": False, "model_called": False, "stages": []}
    tracked = ["scripts/verify_research_quiescence.py", "src/forgeflow/environment.py",
               "src/forgeflow/research_containers.py", "src/forgeflow/run_watchdog.py",
               "src/forgeflow/runner.py", "src/forgeflow/runtime_lifecycle.py"]
    report["source_sha256"] = {n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in tracked}
    process = watchdog = None
    original_ledger = None

    def save():
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")

    def stage(name, **values):
        report["stages"].append({"stage": name, **values})
        save()
        print(json.dumps({"stage": name, **values}), flush=True)

    try:
        with (run_dir / "child.log").open("w") as log:
            process = spawn_managed(
                run_dir / "host-harbor.json",
                [sys.executable, str(Path(__file__).resolve()), "--child", str(run_dir),
                 "--image", image_id, "--project", project],
                "quiescence-acceptance", env=environment, cwd=ROOT,
                stdout=log, stderr=subprocess.STDOUT,
            )
            deadline = time.monotonic() + 150
            while not (run_dir / "ready").exists():
                need(process.poll() is None, "harbor_child_start_failed")
                need(time.monotonic() < deadline, "harbor_child_start_timeout")
                time.sleep(0.1)
            record = json.loads((run_dir / "harbor/job/trial/host-containers.json").read_text())
            original_ledger = record
            ids = _containers(project, environment)
            need(len(ids) == 1, "expected_single_owned_container")
            before = _inspect(ids[0], record, environment)
            need(before["running"], "container_did_not_start")
            stage("actual_harbor_environment_started", container=ids[0])
            watchdog = RunWatchdog(run_dir, agent_sec=1, verifier_sec=1, environment_start_sec=0.3)
            watchdog.start(process, _stop_process_group)
            process.wait(timeout=10)
            watched = watchdog.stop()
            need(watched["status"] == "terminated", "watchdog_did_not_terminate")
            need(wait_host_group(process.pid), "host_group_still_active")
            need(_inspect(ids[0], record, environment)["running"], "missing_residual_container_observation")
            stage("host_stopped_container_still_running", watchdog_reason=watched["reason"])
            result = quiesce_run(run_dir, environment)
            need(result["quiescent"], "container_quiescence_failed")
            stage("owned_container_stopped", result=result)
            _command(["start", ids[0]], environment)
            _command(["pause", ids[0]], environment)
            need(_inspect(ids[0], record, environment)["paused"], "container_not_paused")
            result = quiesce_run(run_dir, environment)
            need(result["quiescent"], "paused_container_quiescence_failed")
            stage("paused_container_stopped", result=result)
            # A changed engine identity must prevent any stop, even with a known ID.
            _command(["start", ids[0]], environment)
            path = run_dir / "harbor/job/trial/host-containers.json"
            path.write_text(json.dumps({**record, "engine_id": "unrelated-engine"}))
            result = quiesce_run(run_dir, environment)
            need(not result["quiescent"], "foreign_engine_was_accepted")
            need(_inspect(ids[0], record, environment)["running"], "foreign_engine_mutated_container")
            path.write_text(json.dumps(record))
            need(quiesce_run(run_dir, environment)["quiescent"], "final_owned_stop_failed")
            stage("changed_engine_refused_without_stopping_container")
            report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error_type=type(error).__name__)
        if isinstance(error, RuntimeError) and str(error).replace("_", "").isalnum():
            report["failure"] = str(error)
    finally:
        if process is not None:
            report["host_stop_errors"] = _stop_process_group(process)
        if watchdog is not None:
            report["watchdog"] = watchdog.stop()
        if original_ledger is not None:
            (run_dir / "harbor/job/trial/host-containers.json").write_text(json.dumps(original_ledger))
        report["final_cleanup"] = quiesce_run(run_dir, environment)
        if not report["final_cleanup"]["quiescent"]:
            report["status"] = "failed"
        report["source_unchanged"] = report["source_sha256"] == {
            n: hashlib.sha256((ROOT / n).read_bytes()).hexdigest() for n in tracked}
        if not report["source_unchanged"]:
            report["status"] = "failed"
        report["private_test_directory"] = str(run_dir)
        save()
    print(json.dumps({"status": report["status"], "cleanup": report["final_cleanup"]["status"]}))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--image", required=True)
    parser.add_argument("--child", type=Path)
    parser.add_argument("--project")
    args = parser.parse_args()
    if args.child:
        asyncio.run(child(args.child, args.image, args.project))
    else:
        need(args.out is not None, "output_required")
        with exclusive_lease(ROOT / ".runtime/lifecycle/heavy-operation.lock"):
            raise SystemExit(acceptance(args.out.resolve(), args.image))

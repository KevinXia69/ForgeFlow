#!/usr/bin/env python3
"""Operator acceptance on disposable qa1/qa2 only; never uses judge invites.

Results stay root-private. Authenticated HTTP uses loopback with the same trusted
HTTPS Host/Origin; this verifies the backends, not the external HTTPS gateway.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import stat
import subprocess
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

import httpx
from reviewer_release import check as check_release
from reviewer_release import snapshot as release_snapshot

from forgeflow.vps_runtime import configuration, trusted_json

STATE = Path("/root/forgeflow-reviewer-acceptance.json")
STAGES = ("prepare", "model", "approve", "isolation", "restart")
_SESSION_COOKIES = {}
TABLES = ("purchase_order", "purchase_order_line", "mrp_production", "stock_picking",
          "stock_move", "stock_move_line", "mrp_workorder", "mrp_workcenter",
          "product_product", "product_template", "stock_quant", "res_partner")


def save(value: dict) -> None:
    value["updated_at"] = datetime.now(UTC).isoformat()
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=STATE.parent,
                                     prefix=STATE.name + ".", suffix=".new", delete=False) as handle:
        temporary = Path(handle.name)
        try:
            json.dump(value, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            os.replace(temporary, STATE)
        finally:
            temporary.unlink(missing_ok=True)


def load_capture(path: Path) -> dict:
    # Captures include resource samples and exceed the config reader's 64 KiB cap.
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(descriptor)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0
                or info.st_mode & 0o077 or info.st_size > 64 * 1024 * 1024):
            raise ValueError("untrusted_acceptance_capture")
        with os.fdopen(descriptor, "r", closefd=False) as handle:
            value = json.load(handle)
        if not isinstance(value, dict):
            raise ValueError("invalid_acceptance_capture")
        return value
    finally:
        os.close(descriptor)


def run(argv: list[str]) -> str:
    result = subprocess.run(argv, capture_output=True, text=True, timeout=30)
    if result.returncode:
        raise RuntimeError("acceptance_subprocess_failed")
    return result.stdout.strip()


def snapshot(config: dict) -> dict:
    sql = " UNION ALL ".join(
        f"SELECT '{table}', count(*)::text, md5(coalesce(string_agg(to_jsonb(t)::text, '|' ORDER BY id), '')) FROM {table} t"
        for table in TABLES)
    output = run(["docker", "exec", config["container"], "runuser", "-u", "postgres", "--",
                  "psql", "-d", "bench", "-XAt", "-F", "|", "-c", sql])
    result = {}
    for line in output.splitlines():
        table, count, digest = line.split("|")
        result[table] = {"count": int(count), "rows_md5": digest}
    if set(result) != set(TABLES):
        raise RuntimeError("business_snapshot_incomplete")
    return result


def client_for(item: dict, host: str):
    client = httpx.Client(base_url=f"http://127.0.0.1:{item['port']}", timeout=940,
                         headers={"Host": host, "Origin": "https://" + host,
                                  "X-Forwarded-Proto": "https"}, trust_env=False)
    # Reuse only this process's login across the extra generation checks. Each
    # instance keeps its own cookie; never retry authentication or a mutation.
    key = (host, item["port"], hashlib.sha256(item["invite_code"].encode()).hexdigest())
    if key in _SESSION_COOKIES:
        client.headers["Cookie"] = _SESSION_COOKIES[key]
        return closing(client)
    response = client.post("/api/auth/session", json={"invite_code": item["invite_code"]})
    if response.status_code != 201 or not response.cookies.get("ff_session"):
        client.close()
        raise RuntimeError("qa_login_failed")
    client.headers["Cookie"] = "ff_session=" + response.cookies["ff_session"]
    _SESSION_COOKIES[key] = client.headers["Cookie"]
    return closing(client)


def request(client: httpx.Client, method: str, path: str, body=None, expected=200):
    response = client.request(method, path, json=body)
    if response.status_code != expected:
        raise RuntimeError("qa_http_status_" + str(response.status_code))
    return response.json()


def resources(configs: dict) -> dict:
    memory = {line.split(":", 1)[0]: int(line.split()[1]) * 1024
              for line in Path("/proc/meminfo").read_text().splitlines()
              if line.startswith(("MemAvailable:", "MemTotal:", "SwapFree:", "SwapTotal:"))}
    units = {}
    for name, config in configs.items():
        values = run(["systemctl", "show", config["service_unit"],
                      "--property=MemoryCurrent,MemoryPeak,CPUUsageNSec,ActiveState,NRestarts"])
        units[name] = dict(line.split("=", 1) for line in values.splitlines() if "=" in line)
    docker = run(["docker", "stats", "--no-stream", "--format", "{{json .}}",
                  *(config["container"] for config in configs.values())])
    container_peaks = {}
    for name, config in configs.items():
        container_id = run(["docker", "inspect", "--format", "{{.Id}}", config["container"]])
        path = Path("/sys/fs/cgroup/system.slice") / ("docker-" + container_id + ".scope") / "memory.peak"
        container_peaks[name] = int(path.read_text()) if path.is_file() else None
    return {"at_unix": time.time(), "memory": memory, "units": units,
            "containers": [json.loads(line) for line in docker.splitlines()],
            "container_memory_peak_bytes": container_peaks}


class Sampler:
    def __init__(self, configs):
        self.configs, self.samples, self.errors = configs, [], 0
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def _run(self):
        while not self.stop.is_set():
            try:
                self.samples.append(resources(self.configs))
            except Exception:
                self.errors += 1
            self.stop.wait(2)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_args):
        self.stop.set()
        self.thread.join(timeout=40)


def prepare(item, config, host, _result, checkpoint):
    before = snapshot(config)
    started = time.monotonic()
    with client_for(item, host) as client:
        environment = request(client, "GET", "/api/environment")
        existing = request(client, "GET", "/api/product/cases")
        if (existing or environment.get("available") is not True
                or environment.get("owned_by_me") is not False or not environment.get("generation")):
            raise RuntimeError("fresh_disposable_qa_environment_required")
        checkpoint({"generation": environment["generation"], "before_prepare": before})
        case = request(client, "POST", "/api/product/cases", {"scenario": "A"}, 201)
        checkpoint({"prepared": case})
    after = snapshot(config)
    checkpoint({"after_prepare": after})
    if case.get("state") != "awaiting_approval" or case.get("execution") is not None:
        raise RuntimeError("qa_preparation_failed")
    if before != after:
        raise RuntimeError("preparation_changed_business_rows")
    return {"prepared": case, "before_prepare": before, "after_prepare": after,
            "prepare_seconds": time.monotonic() - started}


def model(item, _config, host, result, checkpoint):
    started = time.monotonic()
    with client_for(item, host) as client:
        response = request(client, "POST", "/api/product/cases/" + result["prepared"]["case_id"] + "/ask",
                           {"question": "Read this case's evidence. Briefly explain the proposed recovery and whether any ERP write has already occurred.",
                            "conversation_id": "reviewer-isolation-acceptance"})
    checkpoint({"assistant": response, "model_seconds": time.monotonic() - started})
    if (response.get("mode") != "harness" or response.get("source_mode") != "model"
            or not response.get("answer")):
        raise RuntimeError("real_harness_answer_required")
    return {"assistant": response, "model_seconds": time.monotonic() - started}


def approve(item, config, host, result, checkpoint):
    started = time.monotonic()
    original = result["prepared"]
    path = "/api/product/cases/" + original["case_id"] + "/approve"
    payload = {"plan_id": original["plan"]["plan_id"], "comment": "Disposable reviewer acceptance"}
    with client_for(item, host) as client:
        checkpoint({"approval_request": {"case_id": original["case_id"], **payload},
                    "approval_outcome": "pending_or_unknown"})
        case = request(client, "POST", path, payload)
        checkpoint({"executed": case, "approval_outcome": "response_received"})
        execution = case.get("execution") or {}
        verifier = execution.get("verifier") or {}
        if (case.get("state") != "succeeded" or verifier.get("strict_success") is not True
                or verifier.get("evaluation_valid") is not True
                or verifier.get("evaluation_scope") != "product_original_verifier"
                or execution.get("readback", {}).get("execution_validated") is not True):
            raise RuntimeError("qa_real_execution_or_verifier_failed")
        before_duplicate = snapshot(config)
        checkpoint({"before_duplicate": before_duplicate})
        repeated = request(client, "POST", path, payload)
        after_duplicate = snapshot(config)
        checkpoint({"duplicate_response": repeated, "after_duplicate": after_duplicate})
    if repeated.get("execution") != execution or before_duplicate != after_duplicate:
        raise RuntimeError("qa_duplicate_approval_changed_business_state")
    reward = subprocess.run(["docker", "exec", config["container"], "cat", "/logs/verifier/reward.json"],
                            capture_output=True, timeout=30)
    if not reward.returncode:
        checkpoint({"original_reward_json": reward.stdout.decode("utf-8")})
    if reward.returncode or hashlib.sha256(reward.stdout).hexdigest() != verifier.get("reward_sha256"):
        raise RuntimeError("qa_original_reward_binding_failed")
    return {"executed": case, "before_duplicate": before_duplicate, "after_duplicate": after_duplicate,
            "duplicate_idempotent": True, "approve_seconds": time.monotonic() - started,
            "original_reward_json": reward.stdout.decode("utf-8")}


def isolation(index, configs, results, host):
    names = ("qa1", "qa2")
    for position, name in enumerate(names):
        other = names[1 - position]
        with client_for(index[name], host) as client:
            foreign_case = results[other]["prepared"]["case_id"]
            if client.get("/api/product/cases/" + foreign_case).status_code != 404:
                raise RuntimeError("cross_instance_case_visible")
            foreign_url = f"http://127.0.0.1:{index[other]['port']}/api/product/cases"
            if client.get(foreign_url).status_code != 401:
                raise RuntimeError("cross_instance_session_accepted")
        for user, path in (
            (configs[name]["api_user"], configs[other]["case_root"]),
            (configs[name]["dsh_user"], configs[other]["dsh_root"]),
            (configs[name]["api_user"], str(Path(configs[name]["sandbox_root"]) / "api_key")),
            (configs[name]["dsh_user"], str(Path(configs[name]["sandbox_root"]) / "api_key")),
        ):
            attempt = subprocess.run(["runuser", "-u", user, "--", "test", "-r", path],
                                     capture_output=True, timeout=10)
            if attempt.returncode != 1:
                raise RuntimeError("isolated_identity_can_read_protected_state")
    return {"cross_case_404": True, "cross_session_401": True, "unix_file_isolation": True}


def restart(index, configs, results, host):
    before = {name: snapshot(config) for name, config in configs.items()}
    for name, config in configs.items():
        # The service's preserving start must not reseed Odoo or release a lease.
        run(["systemctl", "restart", config["service_unit"]])
        deadline = time.monotonic() + 90
        while time.monotonic() < deadline:
            try:
                with client_for(index[name], host) as client:
                    case = request(client, "GET", "/api/product/cases/" + results[name]["prepared"]["case_id"])
                    if case.get("execution") != results[name]["executed"].get("execution"):
                        raise RuntimeError("restart_changed_execution")
                    environment = request(client, "GET", "/api/environment")
                    if environment.get("owned_by_me") is not True:
                        raise RuntimeError("restart_released_reviewer_lease")
                break
            except httpx.HTTPError:
                time.sleep(1)
        else:
            raise RuntimeError("restart_readiness_timeout")
    after = {name: snapshot(config) for name, config in configs.items()}
    if before != after:
        raise RuntimeError("restart_changed_business_rows")
    return {"before": before, "after": after, "case_execution_preserved": True,
            "leases_preserved": True, "service_restart_passed": True}


def validate_stage(results, stage):
    if results.get("schema") != 2:
        raise RuntimeError("legacy_capture_preserved_choose_new_state_and_fresh_qa")
    stages = results.get("stages", {})
    if stage in stages:
        raise RuntimeError("stage_already_attempted_preserve_results")
    if any(stages.get(previous, {}).get("status") != "passed"
           for previous in STAGES[:STAGES.index(stage)]):
        raise RuntimeError("acceptance_previous_stage_not_passed")


def validate_targets(index, configs):
    if set(index) != set(configs) or set(configs) != {"qa1", "qa2"}:
        raise RuntimeError("only_disposable_qa_instances_allowed")
    for name, config in configs.items():
        if (config.get("instance_name") != name or config.get("api_port") != index[name].get("port")
                or config.get("api_user") != "forgeflow-api-" + name
                or config.get("dsh_user") != "forgeflow-dsh-" + name
                or config.get("service_unit") != f"forgeflow-runtime@{name}.service"):
            raise RuntimeError("qa_target_configuration_mismatch")
    for key in ("api_port", "container", "sandbox_root", "case_root", "socket"):
        if not configs["qa1"].get(key) or configs["qa1"].get(key) == configs["qa2"].get(key):
            raise RuntimeError("qa_targets_not_independent")


def check_generations(index, host, results):
    for name, result in results.items():
        generation = result.get("generation")
        if not generation:
            raise RuntimeError("qa_generation_missing")
        with client_for(index[name], host) as client:
            environment = request(client, "GET", "/api/environment")
            if environment.get("generation") != generation or environment.get("owned_by_me") is not True:
                raise RuntimeError("qa_generation_or_owner_changed")


def execute_stage(stage, results, index, configs, host, revision):
    """Append one attempt. A failed/uncertain write is never retried here."""
    validate_stage(results, stage)
    record = {"status": "running", "started_at": datetime.now(UTC).isoformat()}
    results["stages"][stage] = record
    save(results)
    lock = threading.Lock()

    def checkpoint(name, values):
        with lock:
            results["instances"].setdefault(name, {}).update(values)
            save(results)

    try:
        current = release_snapshot(configs, revision, driver=Path(__file__))
        if stage == "prepare":
            results["release"] = current
            results["source_sha256"] = current["source_sha256"]
            save(results)
        else:
            check_release(results["release"], current)
            check_generations(index, host, results["instances"])
        with Sampler(configs) as sampled:
            try:
                if stage in {"isolation", "restart"}:
                    operation = isolation if stage == "isolation" else restart
                    results[stage] = operation(index, configs, results["instances"], host)
                    save(results)
                else:
                    operation = {"prepare": prepare, "model": model, "approve": approve}[stage]
                    failures = []
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        tasks = {pool.submit(operation, item, configs[name], host,
                                             results["instances"].get(name, {}),
                                             lambda values, name=name: checkpoint(name, values)): name
                                 for name, item in index.items()}
                        for future in as_completed(tasks):
                            name = tasks[future]
                            try:
                                checkpoint(name, future.result())
                            except Exception as error:
                                failures.append(type(error).__name__)
                                checkpoint(name, {"failed_stage": stage, "error_type": type(error).__name__})
                    if failures:
                        raise RuntimeError("acceptance_instance_failed")
            finally:
                results["resources"][stage] = {"samples": sampled.samples,
                                               "sample_errors": sampled.errors}
        check_release(results["release"], release_snapshot(configs, revision, driver=Path(__file__)))
        check_generations(index, host, results["instances"])
        record["status"] = "passed"
    except Exception as error:
        record.update(status="failed", error_type=type(error).__name__)
        raise
    finally:
        record["finished_at"] = datetime.now(UTC).isoformat()
        save(results)


def main():
    global STATE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=STAGES)
    parser.add_argument("--state", type=Path, default=STATE,
                        help="New private capture per QA round; old captures are never overwritten")
    parser.add_argument("--revision", required=True, help="Full clean Git revision frozen for all stages")
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root operator required")
    os.umask(0o077)
    STATE = args.state.absolute()
    if (STATE.is_symlink() or not STATE.parent.is_dir() or STATE.parent.resolve() != STATE.parent
            or STATE.parent.stat().st_uid != 0 or STATE.parent.stat().st_mode & 0o077):
        parser.error("state requires a private operator directory")
    # Serialize stage admission; do not race a second writer against an approval.
    descriptor = os.open(STATE.with_suffix(".lock"), os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as lock:
        info = os.fstat(lock.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o077:
            parser.error("untrusted acceptance lock")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            parser.error("another acceptance stage is running")
        try:
            return run_acceptance(args)
        except Exception as error:
            # Never print request/credential-bearing exception details.
            print(json.dumps({"stage": args.stage, "passed": False,
                              "error_type": type(error).__name__, "capture_preserved": STATE.exists()}))
            return 1


def run_acceptance(args):
    _SESSION_COOKIES.clear()
    private = trusted_json(Path("/etc/forgeflow/reviewers.json"))
    index = {name: private["instances"][name] for name in ("qa1", "qa2")}
    configs = {name: configuration(trusted_json(Path(item["config"]))) for name, item in index.items()}
    validate_targets(index, configs)
    results = load_capture(STATE) if STATE.exists() else {
        "schema": 2, "created_at": datetime.now(UTC).isoformat(),
        "instances": {}, "resources": {}, "stages": {}}
    host = private["trusted_hosts"][0]
    execute_stage(args.stage, results, index, configs, host, args.revision)
    print(json.dumps({"stage": args.stage, "passed": True,
                      "official_reviewer_instances_used": False}), flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

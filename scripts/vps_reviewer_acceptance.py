#!/usr/bin/env python3
"""Operator acceptance on disposable qa1/qa2 only; never uses judge invites.

Results stay root-private. Authenticated HTTP uses loopback with the same trusted
HTTPS Host/Origin; this verifies the backends, not the external HTTPS gateway.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import subprocess
import threading
import time

import httpx

from forgeflow.vps_runtime import configuration, trusted_json

STATE = Path("/root/forgeflow-reviewer-acceptance.json")
TABLES = ("purchase_order", "purchase_order_line", "mrp_production", "stock_picking",
          "stock_move", "stock_move_line", "mrp_workorder", "mrp_workcenter",
          "product_product", "product_template", "stock_quant", "res_partner")


def save(value: dict) -> None:
    temporary = STATE.with_suffix(".new")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, indent=2)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    temporary.chmod(0o600)
    os.replace(temporary, STATE)


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
                                  "X-Forwarded-Proto": "https"})
    response = client.post("/api/auth/session", json={"invite_code": item["invite_code"]})
    if response.status_code != 201 or not response.cookies.get("ff_session"):
        client.close()
        raise RuntimeError("qa_login_failed")
    client.headers["Cookie"] = "ff_session=" + response.cookies["ff_session"]
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


def prepare(item, config, host):
    before = snapshot(config)
    started = time.monotonic()
    with client_for(item, host) as client:
        case = request(client, "POST", "/api/product/cases", {"scenario": "A"}, 201)
    after = snapshot(config)
    if case.get("state") != "awaiting_approval" or case.get("execution") is not None:
        raise RuntimeError("qa_preparation_failed")
    if before != after:
        raise RuntimeError("preparation_changed_business_rows")
    return {"prepared": case, "before_prepare": before, "after_prepare": after,
            "prepare_seconds": time.monotonic() - started}


def model(item, _config, host, result):
    started = time.monotonic()
    with client_for(item, host) as client:
        response = request(client, "POST", "/api/product/cases/" + result["prepared"]["case_id"] + "/ask",
                           {"question": "Read this case's evidence. Briefly explain the proposed recovery and whether any ERP write has already occurred.",
                            "conversation_id": "reviewer-isolation-acceptance"})
    if response.get("mode") != "harness" or not response.get("answer"):
        raise RuntimeError("real_harness_answer_required")
    return {"assistant": response, "model_seconds": time.monotonic() - started}


def approve(item, config, host, result):
    started = time.monotonic()
    original = result["prepared"]
    path = "/api/product/cases/" + original["case_id"] + "/approve"
    payload = {"plan_id": original["plan"]["plan_id"], "comment": "Disposable reviewer acceptance"}
    with client_for(item, host) as client:
        case = request(client, "POST", path, payload)
        execution = case.get("execution") or {}
        verifier = execution.get("verifier") or {}
        if (case.get("state") != "succeeded" or verifier.get("strict_success") is not True
                or verifier.get("evaluation_valid") is not True
                or verifier.get("evaluation_scope") != "product_original_verifier"
                or execution.get("readback", {}).get("execution_validated") is not True):
            raise RuntimeError("qa_real_execution_or_verifier_failed")
        before_duplicate = snapshot(config)
        repeated = request(client, "POST", path, payload)
        after_duplicate = snapshot(config)
    if repeated.get("execution") != execution or before_duplicate != after_duplicate:
        raise RuntimeError("qa_duplicate_approval_changed_business_state")
    reward = subprocess.run(["docker", "exec", config["container"], "cat", "/logs/verifier/reward.json"],
                            capture_output=True, timeout=30)
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


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("prepare", "model", "approve", "isolation", "restart"))
    args = parser.parse_args()
    if os.geteuid() != 0:
        parser.error("root operator required")
    os.umask(0o077)
    private = trusted_json(Path("/etc/forgeflow/reviewers.json"))
    index = {name: private["instances"][name] for name in ("qa1", "qa2")}
    configs = {name: configuration(trusted_json(Path(item["config"]))) for name, item in index.items()}
    results = json.loads(STATE.read_text()) if STATE.exists() else {"schema": 1, "instances": {}, "resources": {}}
    host = private["trusted_hosts"][0]
    if args.stage == "prepare" and results["instances"]:
        raise RuntimeError("acceptance_already_started_preserve_existing_results")
    with Sampler(configs) as sampled:
        if args.stage in {"isolation", "restart"}:
            operation = isolation if args.stage == "isolation" else restart
            results[args.stage] = operation(index, configs, results["instances"], host)
        else:
            operation = {"prepare": prepare, "model": model, "approve": approve}[args.stage]
            with ThreadPoolExecutor(max_workers=2) as pool:
                tasks = {pool.submit(operation, item, configs[name], host,
                                     *([] if args.stage == "prepare" else [results["instances"][name]])): name
                         for name, item in index.items()}
                for future in as_completed(tasks):
                    name = tasks[future]
                    results["instances"].setdefault(name, {}).update(future.result())
                    save(results)
                    print(json.dumps({"instance": name, "stage": args.stage, "passed": True}), flush=True)
    results["resources"][args.stage] = {"samples": sampled.samples, "sample_errors": sampled.errors}
    repo = Path(configs["qa1"]["repo_root"])
    results["source_sha256"] = {str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
                               for path in (Path(__file__), repo / "src/forgeflow/vps_runtime.py",
                                            repo / "src/forgeflow/dsh_launcher.py", repo / "scripts/vps_provision.py",
                                            repo / "scripts/vps_reviewers.py")}
    save(results)
    print(json.dumps({"stage": args.stage, "passed": True, "samples": len(sampled.samples),
                      "official_reviewer_instances_used": False}), flush=True)


if __name__ == "__main__":
    main()

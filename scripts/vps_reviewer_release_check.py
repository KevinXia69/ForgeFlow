#!/usr/bin/env python3
"""Root-only read checks around a reviewer-host reboot and public model stream.

The snapshot command creates login sessions but no cases or ERP writes. The
stream command calls the model against an existing historical case. Neither
command approves or resets an environment. Output contains no invitations,
cookies, model answers or environment secrets.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx

from forgeflow.vps_runtime import trusted_json

CASE_FINGERPRINT_FIELDS = (
    "case_id", "scenario", "state", "synthetic", "order", "disruption", "plan",
    "approval", "execution", "created_at", "updated_at", "reason", "revision",
    "history", "audit",
)


def case_list_fingerprint(cases):
    # Decision flags depend on the clock and live ERP readiness, not persisted state.
    core = [{key: case[key] for key in CASE_FINGERPRINT_FIELDS} for case in cases]
    core.sort(key=lambda case: case["case_id"])
    return hashlib.sha256(json.dumps(core, sort_keys=True).encode()).hexdigest()


def require(condition, label):
    if not condition:
        raise RuntimeError(label)


def run_command(*args):
    return subprocess.check_output(args, text=True, timeout=30).strip()


def authenticate(client, code):
    response = client.post("/api/auth/session", json={"invite_code": code})
    require(response.status_code == 201, "login_failed")
    return response


def snapshot():
    index = trusted_json(Path("/etc/forgeflow/reviewers.json"))
    host = index["trusted_hosts"][0]
    items = [("legacy", index["legacy"])] + [
        (name, index["instances"][name]) for name in (f"judge{i}" for i in range(1, 6))]
    rows = []
    for name, item in items:
        config_path = "/etc/forgeflow/runtime.json" if name == "legacy" else item["config"]
        config = trusted_json(Path(config_path))
        instance = trusted_json(Path(config["sandbox_root"]) / "vps-instance.json")
        with httpx.Client(base_url=f"http://127.0.0.1:{item['port']}",
                          headers={"Host": host, "Origin": "https://" + host,
                                   "X-Forwarded-Proto": "https"},
                          timeout=30, trust_env=False) as client:
            login = authenticate(client, item["invite_code"])
            client.headers["Cookie"] = "ff_session=" + login.cookies["ff_session"]
            env = client.get("/api/environment")
            cases = client.get("/api/product/cases")
            require(env.status_code == cases.status_code == 200, "authenticated_read_failed")
            env = env.json()
            require(env["generation"] == instance["generation"], "generation_mismatch")
            case_data = cases.json()
            if name != "legacy":
                require(env["available"] and not env["owned_by_me"] and case_data == [],
                        "formal_reviewer_not_fresh")
            case_hash = case_list_fingerprint(case_data)
        unit = "forgeflow-runtime.service" if name == "legacy" else f"forgeflow-runtime@{name}.service"
        require(run_command("systemctl", "is-active", unit) == "active", "runtime_inactive")
        require(run_command("systemctl", "is-enabled", unit) == "enabled", "runtime_not_enabled")
        rows.append({"instance": name, "generation": instance["generation"],
                     "case_count": len(case_data), "case_list_sha256": case_hash,
                     "available": env["available"], "owned_by_me": env["owned_by_me"],
                     "service_active": True, "service_enabled": True,
                     "allow_initialize": config["allow_initialize"]})
    units = ["nginx.service", "forgeflow-judge-gateway.service", "forgeflow-cert-renew.timer"]
    active = {unit: run_command("systemctl", "is-active", unit) for unit in units}
    require(all(value == "active" for value in active.values()), "public_service_inactive")
    memory = {line.split(":")[0]: int(line.split()[1]) for line in Path("/proc/meminfo").read_text().splitlines()
              if line.startswith(("MemTotal:", "MemAvailable:", "SwapTotal:", "SwapFree:"))}
    gateway = trusted_json(Path("/etc/forgeflow/gateway.json"))
    require({item["id"] for item in gateway["tenants"]} == {name for name, _ in items},
            "unexpected_gateway_tenants")
    require(gateway["public_tenant"] == "judge1", "unexpected_public_tenant")
    return {"checked_at": datetime.now(UTC).isoformat(),
            "boot_id": Path("/proc/sys/kernel/random/boot_id").read_text().strip(),
            "fingerprint_scope": {"version": 1, "case_fields": list(CASE_FINGERPRINT_FIELDS)},
            "instances": rows, "services": active, "memory_kib": memory,
            "public_tenant": gateway["public_tenant"], "business_writes": False,
            "model_calls": False}


def verify(before_path):
    before = json.loads(before_path.read_text())
    after = snapshot()
    require(before.get("fingerprint_scope") == after["fingerprint_scope"],
            "fingerprint_scope_mismatch")
    require(before["boot_id"] != after["boot_id"], "host_did_not_reboot")
    require(before["instances"] == after["instances"], "instance_state_changed")
    require(all(not item["allow_initialize"] for item in after["instances"]), "initialization_enabled")
    return {"before": before, "after": after, "whole_os_reboot_confirmed": True,
            "generations_cases_and_ownership_preserved": True,
            "all_services_started_automatically": True,
            "new_case_approval_after_reboot_tested": False}


def stream(case_id):
    index = trusted_json(Path("/etc/forgeflow/reviewers.json"))
    host = index["trusted_hosts"][0]
    base = "https://" + host
    started = time.monotonic()
    events = []
    with httpx.Client(base_url=base, headers={"Origin": base}, timeout=960, trust_env=False) as client:
        login = authenticate(client, index["legacy"]["invite_code"])
        require(all(login.cookies.get(name) for name in ("ff_session", "__Host-ff_route")),
                "gateway_cookies_missing")
        response = client.get("/api/product/cases/" + case_id)
        require(response.status_code == 200, "historical_case_unavailable")
        case_before = response.json()
        with client.stream("POST", "/api/product/cases/" + case_id + "/ask/stream",
                           json={"question": "In one sentence, what does this case's execution evidence establish?",
                                 "conversation_id": "reviewer-gateway-release-check"}) as response:
            require(response.status_code == 200 and response.headers.get("content-type", "").startswith("text/event-stream"),
                    "stream_response_failed")
            for line in response.iter_lines():
                if line.startswith("data: "):
                    events.append(json.loads(line[6:]))
        finals = [event for event in events if event.get("type") == "final"]
        errors = [event for event in events if event.get("type") == "error"]
        require(not errors and finals and finals[-1].get("mode") == "harness" and finals[-1].get("answer"),
                "real_model_stream_failed")
        response = client.get("/api/product/cases/" + case_id)
        require(response.status_code == 200, "historical_case_after_unavailable")
        case_after = response.json()
        require(case_before.get("execution") == case_after.get("execution"), "execution_changed")
    return {"checked_at": datetime.now(UTC).isoformat(), "base_url": base,
            "historical_case_id": case_id, "tls_verification": True, "gateway_cookies": True,
            "event_count": len(events), "error_count": 0, "final_mode": "harness",
            "answer_nonempty": True, "answer_sha256": hashlib.sha256(finals[-1]["answer"].encode()).hexdigest(),
            "seconds": round(time.monotonic() - started, 3), "execution_unchanged": True,
            "formal_reviewer_workspaces_used": False, "business_approval_requested": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("stage", choices=("snapshot", "verify", "stream"))
    parser.add_argument("--before", type=Path)
    parser.add_argument("--case-id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    require(os.geteuid() == 0, "root_required")
    os.umask(0o077)
    try:
        if args.stage == "snapshot":
            result = snapshot()
        elif args.stage == "verify":
            require(args.before is not None, "before_checkpoint_required")
            result = verify(args.before)
        else:
            require(args.case_id and len(args.case_id) == 32 and all(c in "0123456789abcdef" for c in args.case_id),
                    "case_id_required")
            result = stream(args.case_id)
        args.output.write_text(json.dumps(result, indent=2) + "\n")
        print(json.dumps({"passed": True, "stage": args.stage}))
        return 0
    except Exception as error:
        # No exception text: network and subprocess diagnostics can include secrets.
        print(json.dumps({"passed": False, "stage": args.stage, "error_type": type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

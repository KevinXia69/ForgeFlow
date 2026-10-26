#!/usr/bin/env python3
"""Allowlisted, credential-free publication of disposable reviewer acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path


def selected(value: dict, keys: str) -> dict:
    return {key: value.get(key) for key in keys.split()}


def export(source: Path, output: Path) -> dict:
    raw = source.read_bytes()
    captured = json.loads(raw)
    if set(captured.get("instances", {})) != {"qa1", "qa2"}:
        raise ValueError("only_disposable_qa_evidence_can_be_exported")
    if not captured.get("restart", {}).get("service_restart_passed"):
        raise ValueError("complete_acceptance_required")
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": 1, "date_utc": "2026-10-26",
              "evidence_kind": "operator_capture_allowlist_not_independent_attestation",
              "execution_kind": "live_odoo_sandbox", "data_kind": "synthetic_erp_bench_non_production",
              "data_profile": "planning-metadata-v1", "backend_transport": "authenticated_loopback_https_host_origin",
              "gateway_tls_validation": "separate_acceptance_required",
              "concurrency": "two_independent_reviewers_per_prepare_model_approve_stage",
              "official_judge_invitations_used": False, "instances": {},
              "source_sha256": captured["source_sha256"],
              "private_capture_sha256": hashlib.sha256(raw).hexdigest()}
    for name, result in captured["instances"].items():
        prepared, executed = result["prepared"], result["executed"]
        execution = executed["execution"]
        verifier = execution["verifier"]
        if (prepared.get("synthetic") is not True or prepared.get("state") != "awaiting_approval"
                or prepared.get("execution") is not None or prepared.get("approval") is not None
                or verifier.get("strict_success") is not True
                or result["before_prepare"] != result["after_prepare"]
                or result["before_duplicate"] != result["after_duplicate"]):
            raise ValueError("acceptance_assertions_missing")
        reward = result["original_reward_json"].encode("utf-8")
        if hashlib.sha256(reward).hexdigest() != verifier["reward_sha256"]:
            raise ValueError("original_reward_hash_mismatch")
        (output / (name + "-original-reward.json")).write_bytes(reward)
        report["instances"][name] = {
            "case_id": prepared["case_id"], "plan_id": prepared["plan"]["plan_id"],
            "before_approval": selected(prepared, "state approval execution updated_at"),
            "business_before_prepare": result["before_prepare"],
            "business_after_prepare": result["after_prepare"],
            "preparation_measurement_scope": "Exact row counts and ordered row-content hashes for the twelve listed business tables; not every Odoo table.",
            "execution": {"state": executed["state"], "status": execution["status"],
                "writes": [selected(item, "document kind quantity") for item in execution["writes"]],
                "readback": selected(execution["readback"], "execution_validated receipt_sha256 readback_sha256 verified_at verified_steps"),
                "verifier": selected(verifier, "status evaluation_scope evaluation_valid strict_success original_passed verification_attempt reward_sha256 score applicable_rule_count passed_rule_count failed_rule_count")},
            "before_duplicate": result["before_duplicate"], "after_duplicate": result["after_duplicate"],
            "duplicate_idempotent": result["duplicate_idempotent"],
            "assistant": {"mode": result["assistant"]["mode"], "answer_present": bool(result["assistant"]["answer"])},
            "timing_seconds": selected(result, "prepare_seconds model_seconds approve_seconds"),
        }
    report["isolation"] = selected(captured["isolation"], "cross_case_404 cross_session_401 unix_file_isolation")
    report["restart"] = selected(captured["restart"], "before after case_execution_preserved leases_preserved service_restart_passed")
    report["resources"] = {}
    for stage, capture in captured["resources"].items():
        samples = []
        for sample in capture["samples"]:
            samples.append({"at_unix": sample["at_unix"],
                "memory": selected(sample["memory"], "MemAvailable MemTotal SwapFree SwapTotal"),
                "units": {name: selected(row, "MemoryCurrent MemoryPeak CPUUsageNSec ActiveState NRestarts")
                          for name, row in sample["units"].items() if name in {"qa1", "qa2"}},
                "containers": [selected(row, "Name CPUPerc MemUsage MemPerc PIDs") for row in sample["containers"]],
                "container_memory_peak_bytes": selected(sample.get("container_memory_peak_bytes", {}), "qa1 qa2")})
        report["resources"][stage] = {"samples": samples, "sample_errors": capture["sample_errors"]}
    (output / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    names = ["acceptance.json", "qa1-original-reward.json", "qa2-original-reward.json"]
    manifest = {"schema": 1, "source_sha256": report["source_sha256"],
                "integrity_scope": "Hashes identify source and evidence bytes; operator captures are not independent attestation.",
                "files": [{"name": name, "bytes": (output / name).stat().st_size,
                           "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in names],
                "redaction": "No invitations, session cookies/secrets, model credentials, model prompts/answers, actor identifiers or private host paths."}
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"public_files_written": len(names) + 1}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(export(arguments.source, arguments.output)))

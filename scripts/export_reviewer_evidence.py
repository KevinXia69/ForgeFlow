#!/usr/bin/env python3
"""Allowlisted, credential-free publication of disposable reviewer acceptance."""
from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

STAGES = ("prepare", "model", "approve", "isolation", "restart")
BINDING_SCOPE = "configured_checkout_package_and_frontend_files_not_process_attestation"


def timestamp(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    except ValueError:
        return None


def capture_date(captured):
    # Never label a historical capture with the export date.
    dates = [timestamp(captured.get(key)) for key in ("created_at", "updated_at")]
    for result in captured.get("instances", {}).values():
        for key in ("prepared", "executed"):
            dates.append(timestamp(result.get(key, {}).get("updated_at")))
    known = [value for value in dates if value is not None]
    return max(known).date().isoformat() if known else None


def hashes(value):
    if not isinstance(value, dict) or not value:
        raise ValueError("release_hashes_required")
    for name, digest in value.items():
        if (not isinstance(name, str) or not name or name.startswith("/")
                or ".." in PurePosixPath(name).parts or "\\" in name
                or any(ord(char) < 32 for char in name)
                or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
            raise ValueError("invalid_release_hashes")
    return dict(value)


def release_evidence(captured):
    schema = captured.get("schema", 1)
    if schema == 1:
        return None, None
    if schema != 2:
        raise ValueError("unsupported_acceptance_schema")
    release = captured.get("release") or {}
    if (release.get("schema") != 1 or release.get("tracked_worktree_clean") is not True
            or not isinstance(release.get("git_revision"), str)
            or re.fullmatch(r"[0-9a-f]{40}", release["git_revision"]) is None
            or release.get("binding_scope") != BINDING_SCOPE):
        raise ValueError("frozen_release_required")
    source = hashes(release.get("source_sha256"))
    frontend = hashes(release.get("frontend_sha256"))
    if "index.html" not in frontend or source != captured.get("source_sha256"):
        raise ValueError("release_hash_binding_mismatch")
    stages = {}
    previous_finish = timestamp(captured.get("created_at"))
    updated = timestamp(captured.get("updated_at"))
    if previous_finish is None or updated is None:
        raise ValueError("capture_timestamps_required")
    for section, fields in (("isolation", ("cross_case_404", "cross_session_401", "unix_file_isolation")),
                            ("restart", ("case_execution_preserved", "leases_preserved", "service_restart_passed"))):
        if any(captured.get(section, {}).get(field) is not True for field in fields):
            raise ValueError("isolation_and_preservation_required")
    for name in STAGES:
        stage = captured.get("stages", {}).get(name, {})
        started, finished = timestamp(stage.get("started_at")), timestamp(stage.get("finished_at"))
        if (stage.get("status") != "passed" or started is None or finished is None
                or not previous_finish <= started <= finished <= updated):
            raise ValueError("complete_version_bound_stages_required")
        stages[name] = selected(stage, "status started_at finished_at")
        previous_finish = finished
    return {"schema": 1, "git_revision": release["git_revision"],
            "tracked_worktree_clean": True, "source_sha256": source,
            "frontend_sha256": frontend, "binding_scope": BINDING_SCOPE}, stages


def require_case_binding(result):
    prepared, executed = result["prepared"], result["executed"]
    case_id = prepared.get("case_id")
    plan_id = (prepared.get("plan") or {}).get("plan_id")
    version = (prepared.get("revision") or {}).get("version")
    assistant = result.get("assistant") or {}
    context = assistant.get("case_context") or {}
    verifier = (executed.get("execution") or {}).get("verifier") or {}
    if (not isinstance(case_id, str) or re.fullmatch(r"[0-9a-f]{32}", case_id) is None
            or not isinstance(plan_id, str) or re.fullmatch(r"[0-9a-f]{64}", plan_id) is None
            or type(version) is not int or version < 1
            or executed.get("case_id") != case_id
            or (executed.get("plan") or {}).get("plan_id") != plan_id
            or (executed.get("revision") or {}).get("version") != version
            or (executed.get("approval") or {}).get("plan_id") != plan_id
            or executed.get("state") != "succeeded"
            or verifier.get("evaluation_valid") is not True
            or verifier.get("evaluation_scope") != "product_original_verifier"
            or executed["execution"].get("readback", {}).get("execution_validated") is not True
            or result.get("duplicate_idempotent") is not True
            or not isinstance(result.get("generation"), str) or not result["generation"]
            or assistant.get("mode") != "harness" or assistant.get("source_mode") != "model"
            or not isinstance(assistant.get("answer"), str) or not assistant["answer"].strip()
            or any(context.get(key) != expected for key, expected in
                   (("case_id", case_id), ("plan_id", plan_id), ("version", version)))):
        raise ValueError("same_case_plan_version_model_and_approval_required")


def selected(value: dict, keys: str) -> dict:
    return {key: value.get(key) for key in keys.split()}


def export(source: Path, output: Path) -> dict:
    raw = source.read_bytes()
    captured = json.loads(raw)
    if set(captured.get("instances", {})) != {"qa1", "qa2"}:
        raise ValueError("only_disposable_qa_evidence_can_be_exported")
    if not captured.get("restart", {}).get("service_restart_passed"):
        raise ValueError("complete_acceptance_required")
    release, stages = release_evidence(captured)
    rewards = {}
    # Check both instances before writing any successful public artifact.
    for name, result in captured["instances"].items():
        prepared, executed = result["prepared"], result["executed"]
        verifier = executed["execution"]["verifier"]
        if (prepared.get("synthetic") is not True or prepared.get("state") != "awaiting_approval"
                or prepared.get("execution") is not None or prepared.get("approval") is not None
                or verifier.get("strict_success") is not True
                or result["before_prepare"] != result["after_prepare"]
                or result["before_duplicate"] != result["after_duplicate"]):
            raise ValueError("acceptance_assertions_missing")
        if release is not None:
            require_case_binding(result)
        reward = result["original_reward_json"].encode("utf-8")
        if hashlib.sha256(reward).hexdigest() != verifier["reward_sha256"]:
            raise ValueError("original_reward_hash_mismatch")
        rewards[name] = reward
    report = {"schema": captured.get("schema", 1), "date_utc": capture_date(captured),
              "evidence_kind": "operator_capture_allowlist_not_independent_attestation",
              "execution_kind": "live_odoo_sandbox", "data_kind": "synthetic_erp_bench_non_production",
              "data_profile": "planning-metadata-v1", "backend_transport": "authenticated_loopback_https_host_origin",
              "gateway_tls_validation": "separate_acceptance_required",
              "concurrency": "two_independent_reviewers_per_prepare_model_approve_stage",
              "official_judge_invitations_used": False, "instances": {},
              "release_binding": "frozen_files" if release is not None else "historical_unknown",
              "provider_usage_or_billing_verified": False,
              "source_sha256": captured["source_sha256"],
              "private_capture_sha256": hashlib.sha256(raw).hexdigest()}
    if release is not None:
        report.update({"release": release, "stages": stages})
    for name, result in captured["instances"].items():
        prepared, executed = result["prepared"], result["executed"]
        execution = executed["execution"]
        verifier = execution["verifier"]
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
            "assistant": {"mode": result["assistant"]["mode"],
                          "source_mode": result["assistant"].get("source_mode"),
                          "answer_present": bool(result["assistant"]["answer"]),
                          "answer_sha256": hashlib.sha256(result["assistant"]["answer"].encode()).hexdigest()},
            "timing_seconds": selected(result, "prepare_seconds model_seconds approve_seconds"),
        }
        if release is not None:
            report["instances"][name].update({"generation": result["generation"],
                "version": prepared["revision"]["version"],
                "approval": selected(executed["approval"], "plan_id issued_at")})
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
    output.mkdir(parents=True, exist_ok=True)
    for name, reward in rewards.items():
        (output / (name + "-original-reward.json")).write_bytes(reward)
    (output / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8", newline="\n")
    names = ["acceptance.json", "qa1-original-reward.json", "qa2-original-reward.json"]
    manifest = {"schema": report["schema"], "source_sha256": report["source_sha256"],
                "release_binding": report["release_binding"],
                "integrity_scope": "Hashes identify source and evidence bytes; operator captures are not independent attestation.",
                "files": [{"name": name, "bytes": (output / name).stat().st_size,
                           "sha256": hashlib.sha256((output / name).read_bytes()).hexdigest()} for name in names],
                "redaction": "No invitations, session cookies/secrets, model credentials, model prompts/answers, actor identifiers or private host paths."}
    if release is not None:
        manifest["release"] = release
    (output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")
    return {"public_files_written": len(names) + 1}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    print(json.dumps(export(arguments.source, arguments.output)))

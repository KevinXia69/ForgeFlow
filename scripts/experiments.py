#!/usr/bin/env python3
"""Plan and analyze P5 experiments and report G0 config/product evidence.

Offline by construction: ``plan`` writes JSON and ``--execute`` only prints
commands; ``analyze``, ``manifest`` and ``report`` read recorded runs. None
starts containers, calls a model, or writes into ``runs/``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import shlex
import statistics
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

TASKS = ("A", "B")
SECURITY_PROFILES = ("none", "permission-split-v1")
DATA_PROFILE = "planning-metadata-v1"
PLANNER_PROFILE = "cp-sat-recovery-v1"
TREATMENT_AGENT = "pi"
CONTROL_AGENT = "nop"
DEFAULT_REPEATS = 3
MIN_REPEATS = 1
MAX_REPEATS = 10
PROTOCOL = "p5-repeat-control-v1"

FINGERPRINT_SCHEMA_VERSION = 3
REPORT_SCHEMA_VERSION = 1
MAX_JSON_BYTES = 4 * 1024 * 1024
SHA256_RE = re.compile(r"[0-9a-f]{64}")
COMMIT_RE = re.compile(r"[0-9a-f]{7,64}")
DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
TASK_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
_MISSING = object()


def _repeat_count(text: str) -> int:
    try:
        value = int(text)
    except ValueError as error:
        raise argparse.ArgumentTypeError("repeats must be an integer") from error
    if not MIN_REPEATS <= value <= MAX_REPEATS:
        raise argparse.ArgumentTypeError(f"repeats must be {MIN_REPEATS}-{MAX_REPEATS}")
    return value


def _entry(
    task: str, agent: str, security: str, data: str, planner: str, repeat: int
) -> dict[str, Any]:
    run_id = f"p5-{task}-{security}-{agent}-r{repeat}"
    command = [
        "uv", "run", "forgeflow", "run", task, "--agent", agent,
        "--security-profile", security, "--data-profile", data,
        "--planner-profile", planner, "--run-id", run_id,
    ]
    return {
        "id": run_id, "task": task, "agent": agent,
        "security_profile": security, "data_profile": data,
        "planner_profile": planner, "command": command, "run_id": run_id,
    }


def build_plan(repeats: int) -> dict[str, Any]:
    """Freeze the A/B matrix: Pi treatments plus one nop control per cell."""
    entries = []
    for task in TASKS:
        for security in SECURITY_PROFILES:
            entries += [
                _entry(task, TREATMENT_AGENT, security, DATA_PROFILE, PLANNER_PROFILE, n)
                for n in range(1, repeats + 1)
            ]
            entries.append(_entry(task, CONTROL_AGENT, security, DATA_PROFILE, "none", 1))
    return {
        "schema_version": 1,
        "protocol": PROTOCOL,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "repeats": repeats,
        "entries": entries,
    }


def _command_plan(out: Path, repeats: int, execute: bool) -> int:
    plan = build_plan(repeats)
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except OSError as error:
        print(f"plan: cannot write {out}: {error}", file=sys.stderr)
        return 2
    entries = plan["entries"]
    pi_count = sum(entry["agent"] == TREATMENT_AGENT for entry in entries)
    print(
        f"plan: {len(entries)} entries ({pi_count} pi, {len(entries) - pi_count} nop), "
        f"repeats={repeats} -> {out}"
    )
    if execute:
        print("execute: printing only; nothing is started.")
        for entry in entries:
            print(f"[{entry['run_id']}] {shlex.join(entry['command'])}")
    return 0


@dataclass(frozen=True)
class _Record:
    run_id: str
    task: str
    agent: str
    security: str
    data: str
    planner: str
    model: str | None
    score: float | None
    valid: bool
    chain_ok: bool | None
    strict: bool | None


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _text(value: object, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _name(value: object, fallback: str, key: str = "name") -> str:
    if isinstance(value, str):
        return _text(value, fallback)
    return _text(value.get(key), fallback) if isinstance(value, dict) else fallback


def _score(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _flag(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _load_run(path: Path) -> _Record | None:
    manifest = _read_json(path / "manifest.json")
    summary = _read_json(path / "summary.json")
    if manifest is None or summary is None:
        return None
    security = "none"
    if "security_profile" in manifest:
        security = _name(manifest["security_profile"], "unknown")
    data = "original"
    if "data_profile" in manifest:
        data = _name(manifest["data_profile"], "unknown")
    elif isinstance(summary.get("data_profile"), str) and summary["data_profile"].strip():
        data = summary["data_profile"]
    planner = "none"
    if "planner" in manifest:
        planner = _name(manifest["planner"], "unknown", "profile")
    model = manifest.get("model")
    model = model.get("model") if isinstance(model, dict) else model
    model = model if isinstance(model, str) and model.strip() else None
    return _Record(
        run_id=_text(manifest.get("run_id"), path.name),
        task=_text(manifest.get("task"), _text(summary.get("task"), "unknown")),
        agent=_text(manifest.get("agent"), _text(summary.get("agent"), "unknown")),
        security=security,
        data=data,
        planner=planner,
        model=model,
        score=_score(summary.get("score")),
        valid=summary.get("evaluation_valid") is True,
        chain_ok=_flag(summary.get("environment_chain_ok")),
        strict=_flag(summary.get("strict_success")),
    )


def _summarize(records: list[_Record]) -> list[dict[str, Any]]:
    groups: dict[tuple[str, str, str, str], list[_Record]] = {}
    for record in records:
        key = (record.task, record.security, record.data, record.agent)
        groups.setdefault(key, []).append(record)
    rows = []
    for key in sorted(groups):
        items = groups[key]
        valid = [record for record in items if record.valid]
        scores = [record.score for record in valid if record.score is not None]
        spread = len(valid) >= 2 and len(scores) >= 2
        models = sorted({record.model or "unrecorded" for record in items})
        planners = sorted({record.planner for record in items})
        rows.append({
            "task": key[0], "security_profile": key[1], "data_profile": key[2],
            "agent": key[3], "runs": len(items), "valid": len(valid),
            "score_mean": statistics.fmean(scores) if scores else None,
            "score_min": min(scores) if spread else None,
            "score_max": max(scores) if spread else None,
            "strict_pass": sum(record.valid and record.strict is True for record in items),
            "mixed_config": len(models) > 1 or len(planners) > 1,
            "models": models, "planner_profiles": planners,
            "environment_chain_failed": sum(record.chain_ok is False for record in items),
            "run_ids": sorted(record.run_id for record in items),
        })
    return rows


_TABLE = (
    ("task", "task"), ("security", "security_profile"), ("data", "data_profile"),
    ("agent", "agent"), ("runs", "runs"), ("valid", "valid"), ("mean", "score_mean"),
    ("min", "score_min"), ("max", "score_max"), ("strict_pass", "strict_pass"),
    ("mixed_config", "mixed_config"), ("chain_fail", "environment_chain_failed"),
)


def _cell(value: object) -> str:
    if value is None:
        return "insufficient"
    if isinstance(value, bool):
        return str(value).lower()
    return f"{value:.2f}" if isinstance(value, float) else str(value)


def _print_table(rows: list[dict[str, Any]], skipped: int) -> None:
    table = [[_cell(row[key]) for _, key in _TABLE] for row in rows]
    widths = [
        max(len(label), *(len(line[index]) for line in table)) if table else len(label)
        for index, (label, _) in enumerate(_TABLE)
    ]
    print("  ".join(label.ljust(widths[i]) for i, (label, _) in enumerate(_TABLE)).rstrip())
    for line in table:
        print("  ".join(line[i].ljust(widths[i]) for i in range(len(line))).rstrip())
    mixed = any(row["mixed_config"] for row in rows)
    chain_ok = not any(row["environment_chain_failed"] for row in rows)
    print(
        f"groups={len(rows)} skipped={skipped} mixed_config={str(mixed).lower()} "
        f"environment_chain_ok={str(chain_ok).lower()}"
    )


def _command_analyze(runs: Path, as_json: bool) -> int:
    if not runs.is_dir():
        print(f"analyze: runs path is not a directory: {runs}", file=sys.stderr)
        return 2
    try:
        children = sorted(runs.iterdir())
    except OSError as error:
        print(f"analyze: cannot list {runs}: {error}", file=sys.stderr)
        return 2
    records, skipped = [], 0
    for child in children:
        if child.is_symlink() or not child.is_dir():
            continue
        record = _load_run(child)
        if record is None:
            skipped += 1
        else:
            records.append(record)
    rows = _summarize(records)
    mixed = any(row["mixed_config"] for row in rows)
    chain_ok = not any(row["environment_chain_failed"] for row in rows)
    if as_json:
        payload = {
            "schema_version": 1, "runs_root": str(runs.resolve()),
            "skipped": skipped, "mixed_config": mixed,
            "environment_chain_ok": chain_ok, "groups": rows,
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        _print_table(rows, skipped)
    return 1 if mixed or not chain_ok else 0


def _optional_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


def _optional_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _optional_number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _optional_count(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _positive_count(value: object) -> int | None:
    return value if type(value) is int and value > 0 else None


def _first_count(container: dict[str, Any], keys: Sequence[str]) -> int | None:
    for key in keys:
        value = _optional_count(container.get(key))
        if value is not None:
            return value
    return None


def _first_number(container: dict[str, Any], keys: Sequence[str]) -> float | int | None:
    for key in keys:
        value = _optional_number(container.get(key))
        if value is not None:
            return value
    return None


def _sha256(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.lower()
    if text.startswith("sha256:"):
        text = text[7:]
    return text if SHA256_RE.fullmatch(text) else None


def _commit_hash(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.lower()
    return text if COMMIT_RE.fullmatch(text) else None


def _canonical_sha256(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _read_run_json(path: Path, label: str, issues: list[str]) -> dict[str, Any] | None:
    if path.is_symlink():
        issues.append(f"{label}: symlink")
        return None
    if not path.is_file():
        issues.append(f"{label}: missing")
        return None
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            issues.append(f"{label}: larger than {MAX_JSON_BYTES} bytes")
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        issues.append(f"{label}: invalid ({type(error).__name__})")
        return None
    if not isinstance(payload, dict):
        issues.append(f"{label}: expected a JSON object")
        return None
    return payload


def _read_optional_json(path: Path) -> dict[str, Any] | None:
    if path.is_symlink() or not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _trial_dir(run_dir: Path) -> Path | None:
    job = run_dir / "harbor" / "job"
    if job.is_symlink() or not job.is_dir():
        return None
    try:
        children = sorted(job.iterdir())
    except OSError:
        return None
    candidates = [
        path for path in children
        if path.is_dir() and not path.is_symlink()
        and (
            (path / "config.json").is_file()
            or (path / "result.json").is_file()
            or (path / "verifier").is_dir()
        )
    ]
    return candidates[0] if len(candidates) == 1 else None


def _recovery_plan_dir(trial: Path | None) -> Path | None:
    if trial is None:
        return None
    directory = trial / "agent" / "recovery"
    if directory.is_symlink() or not directory.is_dir():
        return None
    try:
        candidates = [
            path for path in sorted(directory.iterdir())
            if path.is_dir() and not path.is_symlink() and SHA256_RE.fullmatch(path.name)
        ]
    except OSError:
        return None
    return candidates[0] if len(candidates) == 1 else None


def _task_image_ref(run_dir: Path, task_id: str | None) -> str | None:
    if task_id is None or TASK_RE.fullmatch(task_id) is None:
        return None
    path = run_dir / "task" / task_id / "environment" / "Dockerfile"
    current = run_dir
    for part in ("task", task_id, "environment", "Dockerfile"):
        current /= part
        if current.is_symlink():
            return None
    if not path.is_file():
        return None
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        match = re.match(r"(?i)^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)", line)
        if match is None:
            continue
        ref = match.group(1)
        if "$" in ref or ref.lower() == "scratch":
            return None
        return ref
    return None


def _image_block(manifest: dict[str, Any], run_dir: Path, task_id: str | None) -> dict[str, Any]:
    ref = None
    digest = None
    source = None
    raw = manifest.get("image")
    if isinstance(raw, dict):
        ref = (
            _optional_text(raw.get("ref"))
            or _optional_text(raw.get("reference"))
            or _optional_text(raw.get("name"))
            or _optional_text(raw.get("image"))
        )
        digest = (
            _sha256(raw.get("digest"))
            or _sha256(raw.get("digest_sha256"))
            or _sha256(raw.get("sha256"))
        )
        source = "manifest.image"
    elif isinstance(raw, str):
        ref = _optional_text(raw)
        source = "manifest.image"
    for key in ("image_ref", "container_image", "environment_image", "reference_image"):
        if ref is None:
            ref = _optional_text(manifest.get(key))
            if ref is not None:
                source = f"manifest.{key}"
    for key in ("image_id", "image_digest", "image_sha256"):
        if digest is None:
            digest = _sha256(manifest.get(key))
            if digest is not None and source is None:
                source = f"manifest.{key}"
    docker_ref = _task_image_ref(run_dir, task_id)
    if ref is None and docker_ref is not None:
        ref = docker_ref
        if source is None:
            source = "task/environment/Dockerfile:FROM"
    return {"ref": ref, "digest_sha256": digest, "source": source}


def _own_block(manifest: dict[str, Any]) -> dict[str, Any]:
    commit = None
    commit_source = None
    for key in ("own_commit", "implementation_commit", "repo_commit", "git_commit", "commit"):
        value = _commit_hash(manifest.get(key))
        if value is not None:
            commit, commit_source = value, f"manifest.{key}"
            break
    if commit is None:
        for parent_key in ("implementation", "implementation_snapshot", "source", "git"):
            parent = manifest.get(parent_key)
            if not isinstance(parent, dict):
                continue
            for key in ("commit", "git_commit", "revision"):
                value = _commit_hash(parent.get(key))
                if value is not None:
                    commit, commit_source = value, f"manifest.{parent_key}.{key}"
                    break
            if commit is not None:
                break
    snapshot = manifest.get("implementation_snapshot")
    archive = _sha256(snapshot.get("archive_sha256")) if isinstance(snapshot, dict) else None
    implementation_source = (
        "manifest.implementation_snapshot.archive_sha256" if archive is not None else None
    )
    if archive is None:
        archive = _sha256(manifest.get("implementation_sha256"))
        implementation_source = "manifest.implementation_sha256" if archive is not None else None
    return {
        "commit": commit,
        "commit_source": commit_source,
        "implementation_sha256": archive,
        "implementation_source": implementation_source,
    }


def _upstream_block(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("upstream")
    explicit = _sha256(manifest.get("upstream_lock_sha256"))
    sources = raw
    if isinstance(raw, dict) and isinstance(raw.get("sources"), dict):
        sources = raw["sources"]
    if isinstance(sources, dict) and sources:
        commits = {}
        for name in sorted(sources):
            item = sources[name]
            if isinstance(item, dict):
                commit = _commit_hash(item.get("commit"))
                if commit is not None:
                    commits[name] = commit
        return {
            "lock_sha256": explicit or _canonical_sha256(sources),
            "commits": commits,
            "source": (
                "manifest.upstream_lock_sha256" if explicit is not None
                else "manifest.upstream"
            ),
        }
    if explicit is not None:
        return {"lock_sha256": explicit, "commits": None, "source": "manifest.upstream_lock_sha256"}
    if isinstance(raw, str) and _sha256(raw) is not None:
        return {"lock_sha256": _sha256(raw), "commits": None, "source": "manifest.upstream"}
    return {"lock_sha256": None, "commits": None, "source": None}


def _task_input_block(manifest: dict[str, Any]) -> dict[str, Any]:
    derivation = manifest.get("task_derivation")
    if not isinstance(derivation, dict):
        return {"input_sha256": None, "source": None}
    for key in ("original_file_sha256", "execution_file_sha256"):
        hashes = derivation.get(key)
        if not isinstance(hashes, dict) or not hashes:
            continue
        filtered = {
            name: digest for name, digest in hashes.items()
            if isinstance(name, str) and _sha256(digest) is not None
        }
        if filtered:
            return {
                "input_sha256": _canonical_sha256(filtered),
                "source": f"manifest.task_derivation.{key}",
            }
    return {"input_sha256": None, "source": None}


def _model_block(manifest: dict[str, Any], budget: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("model")
    model_id = None
    provider = None
    if isinstance(raw, dict):
        model_id = (
            _optional_text(raw.get("model"))
            or _optional_text(raw.get("id"))
            or _optional_text(raw.get("name"))
        )
        provider = _optional_text(raw.get("provider"))
    else:
        model_id = _optional_text(raw)
    if model_id is None:
        model_id = _optional_text(manifest.get("model_id"))
    thinking = _optional_text(manifest.get("thinking_mode"))
    if thinking is None and isinstance(raw, dict):
        thinking = _optional_text(raw.get("thinking_mode"))
    sampling = None
    raw_sampling = budget.get("sampling_params")
    if not isinstance(raw_sampling, dict) and isinstance(raw, dict):
        raw_sampling = raw.get("sampling_params")
    if isinstance(raw_sampling, dict):
        sampling = {
            "temperature": _optional_number(raw_sampling.get("temperature")),
            "top_p": _optional_number(raw_sampling.get("top_p")),
        }
    return {
        "id": model_id,
        "provider": provider,
        "thinking_mode": thinking,
        "sampling_params": sampling,
    }


def _budget_block(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("budget")
    recorded = isinstance(raw, dict)
    budget = raw if isinstance(raw, dict) else {}
    return {
        "recorded": recorded,
        "mechanism": _optional_text(budget.get("mechanism")),
        "cleanup_allowance_sec": _optional_number(budget.get("cleanup_allowance_sec")),
        "agent_outer_timeout_sec": _optional_number(budget.get("agent_outer_timeout_sec")),
        "agent_setup_outer_timeout_sec": _optional_number(budget.get("agent_setup_outer_timeout_sec")),
        "agent_timeout_sec": _optional_number(budget.get("agent_timeout_sec")),
        "agent_setup_timeout_sec": _optional_number(budget.get("agent_setup_timeout_sec")),
        "max_output_tokens": _positive_count(budget.get("max_output_tokens")),
        "max_output_tokens_source": _optional_text(budget.get("max_output_tokens_source")),
        "output_policy_version": _optional_text(budget.get("output_policy_version")),
        "output_limit_request_fields": _optional_text(
            budget.get("output_limit_request_fields")
        ),
        "output_budget_scope": _optional_text(budget.get("output_budget_scope")),
        "context_window_tokens": _positive_count(budget.get("context_window_tokens")),
        "attempts": _positive_count(budget.get("attempts")),
        "concurrency": _positive_count(budget.get("concurrency")),
        "max_retries": _optional_count(budget.get("max_retries")),
    }


def _execution_block(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("frozen_execution")
    frozen = raw if isinstance(raw, dict) else {}
    raw_runtime = frozen.get("host_runtime")
    runtime = raw_runtime if isinstance(raw_runtime, dict) else {}
    return {
        "source_copy_recorded": isinstance(raw, dict),
        "execution_uses_snapshot": _optional_bool(frozen.get("execution_uses_snapshot")),
        "source_index_sha256": _sha256(frozen.get("index_sha256")),
        "host_dependencies_frozen": _optional_bool(frozen.get("host_dependencies_frozen")),
        "host_runtime_tree_sha256": _sha256(runtime.get("tree_sha256")),
        "host_lock_sha256": _sha256(runtime.get("lock_sha256")),
        "host_requirements_sha256": _sha256(runtime.get("requirements_sha256")),
        "container_cleanup_mechanism": _optional_text(manifest.get("container_cleanup_mechanism")),
    }


def _data_profile_block(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("data_profile")
    name = None
    content = None
    diff = None
    source = None
    if isinstance(raw, dict):
        name = _optional_text(raw.get("name")) or _optional_text(raw.get("profile"))
        content = (
            _sha256(raw.get("execution_sha256"))
            or _sha256(raw.get("content_sha256"))
            or _sha256(raw.get("sha256"))
            or _sha256(raw.get("digest"))
        )
        diff = _sha256(raw.get("diff_sha256")) or _sha256(raw.get("diff"))
        source = _sha256(raw.get("source_sha256"))
    else:
        name = _optional_text(raw)
    return {
        "name": name,
        "content_sha256": content,
        "diff_sha256": diff,
        "source_sha256": source,
        "recorded": "data_profile" in manifest,
    }


def _profile_block(
    manifest: dict[str, Any], key: str, name_key: str
) -> dict[str, Any]:
    recorded = key in manifest
    raw = manifest.get(key)
    if isinstance(raw, dict):
        alternate = "name" if name_key == "profile" else "profile"
        name = _optional_text(raw.get(name_key)) or _optional_text(raw.get(alternate))
        content = (
            _sha256(raw.get("sha256"))
            or _sha256(raw.get("content_sha256"))
            or _sha256(raw.get("digest"))
        )
    else:
        name = _optional_text(raw)
        content = None
    return {
        "name": name,
        "content_sha256": content,
        "record_sha256": _canonical_sha256(raw) if recorded and raw is not None else None,
        "recorded": recorded,
    }


def _guidance_block(manifest: dict[str, Any], key: str = "execution_guidance") -> dict[str, Any]:
    recorded = key in manifest
    raw = manifest.get(key)
    name = None
    content = None
    version = None
    if isinstance(raw, dict):
        name = _optional_text(raw.get("profile")) or _optional_text(raw.get("name"))
        content = (
            _sha256(raw.get("sha256"))
            or _sha256(raw.get("content_sha256"))
            or _sha256(raw.get("digest"))
        )
        version = _positive_count(raw.get("version"))
    else:
        name = _optional_text(raw)
    return {
        "name": name,
        "content_sha256": content,
        "version": version,
        "recorded": recorded,
    }


def _contract_block(manifest: dict[str, Any]) -> dict[str, Any]:
    raw = manifest.get("contract")
    if not isinstance(raw, dict):
        derivation = manifest.get("task_derivation")
        nested = derivation.get("contract") if isinstance(derivation, dict) else None
        raw = nested if isinstance(nested, dict) else None
    data = raw if isinstance(raw, dict) else {}
    digest = (
        _sha256(data.get("sha256"))
        or _sha256(data.get("digest"))
        or _sha256(data.get("content_sha256"))
    )
    if digest is None:
        digest = (
            _sha256(manifest.get("contract_sha256"))
            or _sha256(manifest.get("contract_digest"))
            or _sha256(manifest.get("contract_content_sha256"))
        )
    source = _optional_text(data.get("source")) or _optional_text(manifest.get("contract_source"))
    enabled = _optional_bool(data.get("enabled"))
    if enabled is None:
        enabled = _optional_bool(manifest.get("contract_enabled"))
    contract_id = _optional_text(data.get("id")) or _optional_text(data.get("contract_id"))
    if contract_id is None:
        contract_id = _optional_text(manifest.get("contract_id"))
    return {
        "id": contract_id,
        "sha256": digest,
        "source": source,
        "enabled": enabled,
    }


def _verifier_block(manifest: dict[str, Any]) -> dict[str, Any]:
    adapter = _optional_text(manifest.get("verifier_adapter"))
    verifier = manifest.get("verifier")
    if adapter is None and isinstance(verifier, dict):
        adapter = (
            _optional_text(verifier.get("import_path"))
            or _optional_text(verifier.get("adapter"))
        )
    digest = _sha256(manifest.get("verifier_sha256")) or _sha256(
        manifest.get("verifier_digest")
    )
    source = "manifest.verifier_sha256" if digest is not None else None
    if digest is None and isinstance(verifier, dict):
        digest = (
            _sha256(verifier.get("sha256"))
            or _sha256(verifier.get("digest"))
            or _sha256(verifier.get("content_sha256"))
        )
        if digest is not None:
            source = "manifest.verifier"
    verifier_version = _optional_text(manifest.get("verifier_adapter_version"))
    if verifier_version is None and isinstance(verifier, dict):
        verifier_version = (
            _optional_text(verifier.get("adapter_version"))
            or _optional_text(verifier.get("version"))
        )
    derivation = manifest.get("task_derivation")
    if digest is None and isinstance(derivation, dict):
        for key in ("original_file_sha256", "execution_file_sha256"):
            hashes = derivation.get(key)
            if not isinstance(hashes, dict) or not hashes:
                continue
            filtered = {
                name: _sha256(value) for name, value in hashes.items()
                if isinstance(name, str) and name.startswith("tests/")
                and _sha256(value) is not None
            }
            if filtered:
                digest = _canonical_sha256(filtered)
                source = f"manifest.task_derivation.{key}"
                break
    return {
        "adapter": adapter,
        "adapter_version": verifier_version,
        "content_sha256": digest,
        "source": source,
    }


def _normalize_approval_mode(value: str | None) -> str | None:
    if value is None:
        return None
    lowered = value.lower()
    if (
        lowered in ("evaluation", "eval")
        or lowered.startswith("benchmark")
        or lowered.startswith("environment_control")
        or lowered.startswith("evaluation_")
    ):
        return "evaluation"
    if "product" in lowered or "approval" in lowered:
        return "product_approval"
    return value


def _approval_block(manifest: dict[str, Any]) -> dict[str, Any]:
    approval = manifest.get("approval")
    if isinstance(approval, dict):
        required = _optional_bool(approval.get("required"))
        mode = _normalize_approval_mode(_optional_text(approval.get("mode")))
        if mode is None and required is True:
            mode = "product_approval"
        if mode is not None or required is not None:
            return {"mode": mode, "required": required, "source": "manifest.approval"}
    mode = _normalize_approval_mode(
        _optional_text(manifest.get("approval_mode"))
        or _optional_text(manifest.get("evaluation_mode"))
    )
    required = _optional_bool(manifest.get("approval_required"))
    product_approval = _optional_bool(manifest.get("product_approval"))
    if product_approval is True and mode is None:
        mode = "product_approval"
    if mode is not None or required is not None:
        return {"mode": mode, "required": required, "source": "manifest.approval_mode"}
    recorded_mode = _optional_text(manifest.get("mode"))
    if recorded_mode is None:
        return {"mode": None, "required": None, "source": None}
    lowered = recorded_mode.lower()
    if lowered.startswith("benchmark") or lowered.startswith("environment_control"):
        return {"mode": "evaluation", "required": None, "source": "manifest.mode"}
    if lowered in ("evaluation", "eval"):
        return {"mode": "evaluation", "required": None, "source": "manifest.mode"}
    if "product" in lowered or "approval" in lowered:
        return {"mode": "product_approval", "required": None, "source": "manifest.mode"}
    return {"mode": None, "required": None, "source": "manifest.mode:unrecognized"}


def _valid_date(value: object) -> str | None:
    text = _optional_text(value)
    if text is None or DATE_RE.fullmatch(text) is None:
        return None
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except ValueError:
        return None
    return text


def _business_block(manifest: dict[str, Any], run_dir: Path) -> dict[str, Any]:
    anchor = None
    anchor_source = None
    snapshot = None
    snapshot_source = None
    business = manifest.get("business")
    business = business if isinstance(business, dict) else {}
    for key in ("business_anchor_date", "business_anchor", "anchor_date"):
        anchor = _valid_date(manifest.get(key))
        if anchor is not None:
            anchor_source = f"manifest.{key}"
            break
    if anchor is None:
        anchor = _valid_date(business.get("anchor_date")) or _valid_date(
            business.get("anchor")
        )
        if anchor is not None:
            anchor_source = "manifest.business.anchor_date"
    for key in ("start_snapshot_sha256", "start_snapshot_digest"):
        snapshot = _sha256(manifest.get(key))
        if snapshot is not None:
            snapshot_source = f"manifest.{key}"
            break
    if snapshot is None:
        raw_snapshot = manifest.get("start_snapshot")
        if isinstance(raw_snapshot, str):
            snapshot = _sha256(raw_snapshot)
        elif isinstance(raw_snapshot, dict):
            snapshot = (
                _sha256(raw_snapshot.get("sha256"))
                or _sha256(raw_snapshot.get("snapshot_sha256"))
            )
        if snapshot is not None:
            snapshot_source = "manifest.start_snapshot"
    if snapshot is None:
        snapshot = _sha256(business.get("start_snapshot_sha256")) or _sha256(
            business.get("snapshot_sha256")
        )
        if snapshot is not None:
            snapshot_source = "manifest.business.start_snapshot_sha256"
    if snapshot is None:
        raw_snapshot = business.get("start_snapshot")
        if isinstance(raw_snapshot, str):
            snapshot = _sha256(raw_snapshot)
        elif isinstance(raw_snapshot, dict):
            snapshot = (
                _sha256(raw_snapshot.get("sha256"))
                or _sha256(raw_snapshot.get("snapshot_sha256"))
            )
        if snapshot is not None:
            snapshot_source = "manifest.business.start_snapshot"
    contract = manifest.get("contract")
    if anchor is None and isinstance(contract, dict):
        anchor = _valid_date(contract.get("anchor_date"))
        if anchor is not None:
            anchor_source = "manifest.contract.anchor_date"
    if anchor is None or snapshot is None:
        plan = _recovery_plan_dir(_trial_dir(run_dir))
        if plan is not None:
            request = _read_optional_json(plan / "request.json")
            if anchor is None and isinstance(request, dict):
                anchor = _valid_date(request.get("anchor_date"))
                if anchor is not None:
                    anchor_source = "harbor/job:agent/recovery/request.json.anchor_date"
            if snapshot is None:
                binding = _read_optional_json(plan / "binding.json")
                if isinstance(binding, dict):
                    snapshot = _sha256(binding.get("snapshot_sha256"))
                    if snapshot is not None:
                        snapshot_source = "harbor/job:agent/recovery/binding.json.snapshot_sha256"
            if snapshot is None:
                recovery_manifest = _read_optional_json(plan / "manifest.json")
                artifacts = (
                    recovery_manifest.get("artifacts")
                    if isinstance(recovery_manifest, dict) else None
                )
                if isinstance(artifacts, dict):
                    snapshot = _sha256(artifacts.get("snapshot"))
                    if snapshot is not None:
                        snapshot_source = "harbor/job:agent/recovery/manifest.json.artifacts.snapshot"
    return {
        "anchor_date": anchor,
        "anchor_source": anchor_source,
        "start_snapshot_sha256": snapshot,
        "snapshot_source": snapshot_source,
    }


def _explicit_tool_calls(
    manifest: dict[str, Any], summary: dict[str, Any]
) -> tuple[int | None, str | None]:
    containers = (
        (summary.get("metrics"), "summary.metrics"),
        (summary, "summary"),
        (manifest.get("metrics"), "manifest.metrics"),
        (manifest, "manifest"),
    )
    for container, label in containers:
        if not isinstance(container, dict):
            continue
        for key in ("tool_calls", "tool_call_count", "n_tool_calls"):
            count = _optional_count(container.get(key))
            if count is not None:
                return count, f"{label}.{key}"
    activity = summary.get("tool_activity")
    if isinstance(activity, dict):
        count = _optional_count(activity.get("tool_call_count"))
        if count is not None:
            return count, "summary.tool_activity.tool_call_count"
    return None, None


def _trajectory_tool_calls(run_dir: Path) -> tuple[int | None, str | None]:
    trial = _trial_dir(run_dir)
    if trial is None:
        return None, None
    path = trial / "agent" / "pi.txt"
    if path.is_symlink() or not path.is_file():
        return None, None
    count = 0
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if "tool_execution_start" not in line:
                    continue
                try:
                    payload = json.loads(line)
                except ValueError:
                    continue
                if isinstance(payload, dict) and payload.get("type") == "tool_execution_start":
                    count += 1
    except OSError:
        return None, None
    return count, "agent/pi.txt:tool_execution_start"


def _tool_calls(
    run_dir: Path, manifest: dict[str, Any], summary: dict[str, Any]
) -> tuple[int | None, str | None]:
    count, source = _explicit_tool_calls(manifest, summary)
    if count is not None:
        return count, source
    return _trajectory_tool_calls(run_dir)


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _time_delta(started: object, finished: object) -> float | None:
    first = _parse_time(started)
    second = _parse_time(finished)
    if first is None or second is None:
        return None
    if (first.tzinfo is None) != (second.tzinfo is None):
        return None
    duration = (second - first).total_seconds()
    return round(duration, 3) if duration >= 0 else None


def _elapsed_seconds(
    run_dir: Path, manifest: dict[str, Any]
) -> tuple[float | None, str | None, float | None]:
    total = _time_delta(manifest.get("started_at_utc"), manifest.get("finished_at_utc"))
    total_source = "manifest.started_at_utc/finished_at_utc" if total is not None else None
    trial = _trial_dir(run_dir)
    result = _read_optional_json(trial / "result.json") if trial is not None else None
    if total is None and isinstance(result, dict):
        total = _time_delta(result.get("started_at"), result.get("finished_at"))
        total_source = "harbor/job:trial/result.json" if total is not None else None
    agent_elapsed = None
    agent_source = None
    if isinstance(result, dict):
        phase = result.get("agent_execution")
        if isinstance(phase, dict):
            agent_elapsed = _time_delta(phase.get("started_at"), phase.get("finished_at"))
            if agent_elapsed is not None:
                agent_source = "harbor/job:trial/result.json.agent_execution"
    if agent_elapsed is not None:
        return agent_elapsed, agent_source, total
    return total, total_source, total


def _error_type(payload: object) -> str | None:
    if not isinstance(payload, dict):
        return None
    return _optional_text(payload.get("exception_type")) or _optional_text(payload.get("type"))


def _timeout_flag(
    state: str | None, manifest: dict[str, Any], summary: dict[str, Any]
) -> bool | None:
    if state in ("timeout", "timed_out"):
        return True
    for payload in (
        summary.get("trial_exception"),
        summary.get("runner_error"),
        manifest.get("error"),
    ):
        if not isinstance(payload, dict):
            continue
        exception_type = (_error_type(payload) or "").lower()
        message = (
            _optional_text(payload.get("exception_message"))
            or _optional_text(payload.get("message"))
            or ""
        ).lower()
        if "timeout" in exception_type or "timed out" in message:
            return True
    return False if state is not None else None


def _failure_class(name: str | None) -> str:
    if name is None or not name.strip():
        return "unknown"
    lowered = name.lower()
    if "timeout" in lowered or "timedout" in lowered:
        return "timeout"
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()
    snake = re.sub(r"[^a-z0-9]+", "_", snake).strip("_")
    return snake[:80] or "unknown"


def _failure_error_type(manifest: dict[str, Any], summary: dict[str, Any]) -> str | None:
    for payload in (
        summary.get("trial_exception"),
        summary.get("runner_error"),
        manifest.get("error"),
    ):
        value = _error_type(payload)
        if value is not None:
            return value
    return None


def _failure_info(
    manifest: dict[str, Any], summary: dict[str, Any], product: dict[str, Any]
) -> tuple[str | None, str | None]:
    state = product["state"]
    runner_error = summary.get("runner_error")
    if not isinstance(runner_error, dict):
        runner_error = manifest.get("error")
    if isinstance(runner_error, dict):
        exception_type = _error_type(runner_error)
        if exception_type is not None:
            failure_class = _failure_class(exception_type)
            stage = "agent" if failure_class == "timeout" else "runner"
            return stage, failure_class
    trial_exception = summary.get("trial_exception")
    if isinstance(trial_exception, dict):
        exception_type = _error_type(trial_exception)
        if exception_type is not None:
            failure_class = _failure_class(exception_type)
            if failure_class == "timeout":
                return "agent", failure_class
            lowered = exception_type.lower()
            if "verifier" in lowered:
                return "verifier", failure_class
            if "agent" in lowered:
                return "agent", failure_class
            return "trial", failure_class
    if state in ("timeout", "timed_out"):
        return "agent", "timeout"
    if state in ("failed", "interrupted"):
        return "runner", "run_failed"
    if product["environment_chain_ok"] is False:
        return "verifier", "evaluation_invalid"
    if product["evaluation_valid"] is False:
        return "verifier", "evaluation_invalid"
    if product["over_budget"] is True:
        return "budget", "over_budget"
    if (
        state == "completed"
        and product["agent"] == "pi"
        and product["evaluation_valid"] is True
        and product["strict_success"] is False
    ):
        return "business", "strict_not_met"
    return None, None


def _completed_within_budget(
    state: str | None,
    evaluation_valid: bool | None,
    elapsed: float | None,
    budget: float | None,
    timeout: bool | None,
) -> bool | None:
    if timeout is True or state in ("failed", "interrupted", "timeout", "timed_out"):
        return False
    if state != "completed":
        return None
    if elapsed is not None and budget is not None and elapsed > budget:
        return False
    if evaluation_valid is False:
        return False
    if evaluation_valid is not True:
        return None
    if elapsed is None or budget is None:
        return None
    return True


def _product(
    run_dir: Path, manifest: dict[str, Any], summary: dict[str, Any]
) -> dict[str, Any]:
    state = _optional_text(summary.get("state")) or _optional_text(manifest.get("state"))
    agent = _optional_text(manifest.get("agent")) or _optional_text(summary.get("agent"))
    mode = _optional_text(summary.get("mode")) or _optional_text(manifest.get("mode"))
    evaluation_valid = _optional_bool(summary.get("evaluation_valid"))
    environment_chain_ok = _optional_bool(summary.get("environment_chain_ok"))
    strict_success = _optional_bool(summary.get("strict_success"))
    budget_record = manifest.get("budget")
    budget = (
        _optional_number(budget_record.get("agent_timeout_sec"))
        if isinstance(budget_record, dict) else None
    )
    elapsed, elapsed_source, total_elapsed = _elapsed_seconds(run_dir, manifest)
    timeout = _timeout_flag(state, manifest, summary)
    over_budget = (
        elapsed > budget if elapsed is not None and budget is not None else None
    )
    metrics = summary.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    cost = _first_number(metrics, ("cost_usd", "actual_cost"))
    if cost is not None and cost < 0:
        cost = None
    tool_calls, tool_calls_source = _tool_calls(run_dir, manifest, summary)
    product = {
        "attempted": True,
        "state": state,
        "agent": agent,
        "mode": mode,
        "evaluation_valid": evaluation_valid,
        "environment_chain_ok": environment_chain_ok,
        "strict_success": strict_success,
        "completed_within_budget": _completed_within_budget(
            state, evaluation_valid, elapsed, budget, timeout
        ),
        "over_budget": over_budget,
        "timeout": timeout,
        "budget_seconds": budget,
        "elapsed_seconds": elapsed,
        "elapsed_source": elapsed_source,
        "total_elapsed_seconds": total_elapsed,
        "failure_stage": None,
        "failure_class": None,
        "failure_error_type": _failure_error_type(manifest, summary),
        "tool_calls": tool_calls,
        "tool_calls_source": tool_calls_source,
        "input_tokens": _first_count(metrics, ("n_input_tokens", "input_tokens")),
        "output_tokens": _first_count(metrics, ("n_output_tokens", "output_tokens")),
        "cache_tokens": _first_count(metrics, ("n_cache_tokens", "cache_tokens")),
        "actual_cost": cost,
    }
    product["failure_stage"], product["failure_class"] = _failure_info(
        manifest, summary, product
    )
    return product


def _upstream_view(manifest: dict[str, Any], summary: dict[str, Any]) -> dict[str, Any]:
    error = summary.get("trial_exception")
    if not isinstance(error, dict):
        error = summary.get("runner_error")
    if not isinstance(error, dict):
        error = manifest.get("error")
    raw_issues = summary.get("issues")
    issues = (
        [str(item)[:300] for item in raw_issues][:20]
        if isinstance(raw_issues, list) else []
    )
    return {
        "state": _optional_text(summary.get("state")) or _optional_text(manifest.get("state")),
        "mode": _optional_text(summary.get("mode")) or _optional_text(manifest.get("mode")),
        "evaluation_valid": _optional_bool(summary.get("evaluation_valid")),
        "environment_chain_ok": _optional_bool(summary.get("environment_chain_ok")),
        "strict_success": _optional_bool(summary.get("strict_success")),
        "score": _score(summary.get("score")),
        "original_passed": _optional_bool(summary.get("original_passed")),
        "harbor_returncode": (
            summary.get("harbor_returncode")
            if type(summary.get("harbor_returncode")) is int else None
        ),
        "error_type": _error_type(error),
        "issues": issues,
    }


def _fingerprint_record(run_dir: Path) -> dict[str, Any]:
    issues: list[str] = []
    manifest = _read_run_json(run_dir / "manifest.json", "manifest.json", issues) or {}
    summary = _read_run_json(run_dir / "summary.json", "summary.json", issues) or {}
    task_id = (
        _optional_text(manifest.get("task"))
        or _optional_text(manifest.get("task_id"))
        or _optional_text(summary.get("task"))
    )
    agent = _optional_text(manifest.get("agent")) or _optional_text(summary.get("agent"))
    budget_raw = manifest.get("budget")
    budget = budget_raw if isinstance(budget_raw, dict) else {}
    task_input = _task_input_block(manifest)
    fingerprint = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "agent": agent,
        "source": _own_block(manifest),
        "execution": _execution_block(manifest),
        "upstream": _upstream_block(manifest),
        "environment": _image_block(manifest, run_dir, task_id),
        "task": {
            "id": task_id,
            "input_sha256": task_input["input_sha256"],
            "input_source": task_input["source"],
        },
        "model": _model_block(manifest, budget),
        "budget": _budget_block(manifest),
        "profiles": {
            "data": _data_profile_block(manifest),
            "planner": _profile_block(manifest, "planner", "profile"),
            "security": _profile_block(manifest, "security_profile", "name"),
            "guidance": _guidance_block(manifest),
            "contract_guidance": _guidance_block(manifest, "contract_guidance"),
        },
        "contract": _contract_block(manifest),
        "verifier": _verifier_block(manifest),
        "approval": _approval_block(manifest),
        "business": _business_block(manifest, run_dir),
    }
    return {
        "run_id": (
            _optional_text(manifest.get("run_id"))
            or _optional_text(summary.get("run_id"))
            or run_dir.name
        ),
        "run_dir": str(run_dir),
        "fingerprint": fingerprint,
        "product": _product(run_dir, manifest, summary),
        "upstream": _upstream_view(manifest, summary),
        "issues": sorted(issues),
    }


def _looks_like_runs_root(candidate: Path) -> bool:
    try:
        children = sorted(candidate.iterdir())
    except OSError:
        return False
    return any(
        child.is_dir() and not child.is_symlink()
        and ((child / "manifest.json").exists() or (child / "summary.json").exists())
        for child in children
    )


def _validate_run_dirs(paths: Sequence[Path], command: str) -> list[Path] | None:
    resolved: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path.is_symlink() or not path.is_dir():
            print(f"{command}: not a run directory: {path}", file=sys.stderr)
            return None
        candidate = path.resolve()
        if candidate in seen:
            continue
        seen.add(candidate)
        has_run_file = (candidate / "manifest.json").exists() or (
            candidate / "summary.json"
        ).exists()
        if not has_run_file and _looks_like_runs_root(candidate):
            print(
                f"{command}: looks like a runs root, not a run directory: {path}",
                file=sys.stderr,
            )
            return None
        resolved.append(candidate)
    return resolved


def _flatten_fingerprint(value: object, prefix: str = "") -> dict[str, object]:
    flat: dict[str, object] = {}
    if isinstance(value, dict):
        if not value:
            flat[prefix] = {}
        for key in sorted(value):
            child = f"{prefix}.{key}" if prefix else key
            flat.update(_flatten_fingerprint(value[key], child))
    elif isinstance(value, list):
        flat[prefix] = value
    else:
        flat[prefix] = value
    return flat


def _comparable(value: object) -> str:
    if value is None:
        return "<unrecorded>"
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _mixed_config(records: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    flattened = [_flatten_fingerprint(record["fingerprint"]) for record in records]
    paths: set[str] = set()
    for item in flattened:
        paths.update(item)
    differing = []
    for path in sorted(paths):
        values = set()
        for item in flattened:
            value = item.get(path, _MISSING)
            values.add("<missing>" if value is _MISSING else _comparable(value))
        if len(values) > 1:
            differing.append(path)
    return bool(differing), differing


def _output_allowed(out: Path, run_dirs: Sequence[Path]) -> bool:
    resolved = out.resolve()
    working_runs = (Path.cwd() / "runs").resolve()
    if resolved == working_runs or working_runs in resolved.parents:
        return False
    for run_dir in run_dirs:
        if resolved == run_dir or run_dir in resolved.parents:
            return False
        runs_root = run_dir.parent
        if runs_root.name == "runs" and (
            resolved == runs_root or runs_root in resolved.parents
        ):
            return False
    return True


def _write_payload(
    out: Path, payload: dict[str, Any], run_dirs: Sequence[Path], command: str
) -> bool:
    if not _output_allowed(out, run_dirs):
        print(
            f"{command}: refusing to write JSON inside a run directory or runs root: {out}",
            file=sys.stderr,
        )
        return False
    try:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    except OSError as error:
        print(f"{command}: cannot write {out}: {error}", file=sys.stderr)
        return False
    return True


def _display(value: object) -> str:
    if value is None:
        return "unrecorded"
    if isinstance(value, bool):
        return str(value).lower()
    if isinstance(value, float):
        return f"{value:g}"
    return str(value)


def _short(value: object, limit: int = 12) -> str:
    text = _display(value)
    return text if len(text) <= limit else text[:limit] + "..."


def _format_table(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> list[str]:
    widths = [len(header) for header in headers]
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], len(cell))
    lines = [
        "  ".join(header.ljust(widths[index]) for index, header in enumerate(headers)).rstrip()
    ]
    for row in rows:
        lines.append(
            "  ".join(cell.ljust(widths[index]) for index, cell in enumerate(row)).rstrip()
        )
    return lines


def _pair_text(mapping: dict[str, int]) -> str:
    if not mapping:
        return "none"
    return ", ".join(f"{key}={value}" for key, value in sorted(mapping.items()))


def _print_manifest_table(
    records: list[dict[str, Any]], mixed: bool, mixed_fields: list[str]
) -> None:
    headers = (
        "run_id", "task", "agent", "model", "thinking", "profiles", "budget_s",
        "commit", "upstream", "verifier", "approval",
    )
    rows = []
    for record in records:
        fingerprint = record["fingerprint"]
        profiles = fingerprint["profiles"]
        rows.append([
            record["run_id"],
            _display(fingerprint["task"]["id"]),
            _display(fingerprint["agent"]),
            _display(fingerprint["model"]["id"]),
            _display(fingerprint["model"]["thinking_mode"]),
            "/".join(_display(profiles[key]["name"]) for key in (
                "data", "planner", "security", "guidance", "contract_guidance",
            )),
            _display(fingerprint["budget"]["agent_timeout_sec"]),
            _short(fingerprint["source"]["commit"]),
            _short(fingerprint["upstream"]["lock_sha256"]),
            _short(fingerprint["verifier"]["content_sha256"]),
            _display(fingerprint["approval"]["mode"]),
        ])
    for line in _format_table(headers, rows):
        print(line)
    print(f"runs={len(records)} mixed_config={str(mixed).lower()}")
    if mixed_fields:
        print("mixed_fields=" + ", ".join(mixed_fields))
    issue_count = sum(bool(record["issues"]) for record in records)
    if issue_count:
        print(f"runs_with_read_issues={issue_count}")


def _stats_entry() -> dict[str, Any]:
    return {
        "attempted": 0,
        "evaluation_valid": 0,
        "strict_success": 0,
        "completed_within_budget": 0,
        "within_budget_unknown": 0,
        "invalid": 0,
        "valid_unknown": 0,
        "strict_unknown": 0,
        "timeout": 0,
        "failures": 0,
        "failure_stages": {},
        "failure_classes": {},
        "failure_pairs": {},
        "run_ids": [],
    }


def _add_stats(stats: dict[str, Any], record: dict[str, Any]) -> None:
    product = record["product"]
    stats["attempted"] += 1
    stats["run_ids"].append(record["run_id"])
    if product["evaluation_valid"] is True:
        stats["evaluation_valid"] += 1
    elif product["evaluation_valid"] is False:
        stats["invalid"] += 1
    else:
        stats["valid_unknown"] += 1
    if product["strict_success"] is True:
        stats["strict_success"] += 1
    elif product["strict_success"] is None:
        stats["strict_unknown"] += 1
    if product["completed_within_budget"] is True:
        stats["completed_within_budget"] += 1
    elif product["completed_within_budget"] is None:
        stats["within_budget_unknown"] += 1
    if product["timeout"] is True:
        stats["timeout"] += 1
    stage = product["failure_stage"]
    failure_class = product["failure_class"]
    if stage is not None:
        stats["failures"] += 1
        stats["failure_stages"][stage] = stats["failure_stages"].get(stage, 0) + 1
        key = f"{stage}:{failure_class or 'unknown'}"
        stats["failure_pairs"][key] = stats["failure_pairs"].get(key, 0) + 1
    if failure_class is not None:
        stats["failure_classes"][failure_class] = (
            stats["failure_classes"].get(failure_class, 0) + 1
        )


def _stats_payload(stats: dict[str, Any], include_runs: bool = True) -> dict[str, Any]:
    payload = {
        "attempted": stats["attempted"],
        "evaluation_valid": stats["evaluation_valid"],
        "strict_success": stats["strict_success"],
        "completed_within_budget": stats["completed_within_budget"],
        "within_budget_unknown": stats["within_budget_unknown"],
        "invalid": stats["invalid"],
        "valid_unknown": stats["valid_unknown"],
        "strict_unknown": stats["strict_unknown"],
        "timeout": stats["timeout"],
        "failures": stats["failures"],
        "failure_stages": dict(sorted(stats["failure_stages"].items())),
        "failure_classes": dict(sorted(stats["failure_classes"].items())),
        "failure_pairs": dict(sorted(stats["failure_pairs"].items())),
    }
    if include_runs:
        payload["run_ids"] = sorted(stats["run_ids"])
    return payload


def _command_manifest(run_dirs: Sequence[Path], out: Path | None) -> int:
    paths = _validate_run_dirs(run_dirs, "manifest")
    if paths is None:
        return 2
    if out is not None and not _output_allowed(out, paths):
        print(
            f"manifest: refusing to write JSON inside a run directory or runs root: {out}",
            file=sys.stderr,
        )
        return 2
    records = sorted(
        (_fingerprint_record(path) for path in paths),
        key=lambda record: (record["run_id"], record["run_dir"]),
    )
    mixed, mixed_fields = _mixed_config(records)
    payload = {
        "schema_version": FINGERPRINT_SCHEMA_VERSION,
        "kind": "forgeflow.g0_config_fingerprints",
        "mixed_config": mixed,
        "mixed_fields": mixed_fields,
        "runs": records,
    }
    if out is not None and not _write_payload(out, payload, paths, "manifest"):
        return 2
    _print_manifest_table(records, mixed, mixed_fields)
    return 1 if mixed else 0


def _command_report(run_dirs: Sequence[Path], out: Path | None) -> int:
    paths = _validate_run_dirs(run_dirs, "report")
    if paths is None:
        return 2
    if out is not None and not _output_allowed(out, paths):
        print(
            f"report: refusing to write JSON inside a run directory or runs root: {out}",
            file=sys.stderr,
        )
        return 2
    records = sorted(
        (_fingerprint_record(path) for path in paths),
        key=lambda record: (record["run_id"], record["run_dir"]),
    )
    mixed, mixed_fields = _mixed_config(records)
    groups: dict[tuple[str, str, str], dict[str, Any]] = {}
    totals = _stats_entry()
    for record in records:
        fingerprint = record["fingerprint"]
        product = record["product"]
        key = (
            fingerprint["task"]["id"] or "unrecorded",
            fingerprint["agent"] or "unrecorded",
            product["mode"] or "unrecorded",
        )
        group = groups.get(key)
        if group is None:
            group = _stats_entry()
            group.update(task=key[0], agent=key[1], mode=key[2])
            groups[key] = group
        _add_stats(group, record)
        _add_stats(totals, record)
    headers = (
        "task", "agent", "mode", "attempted", "valid", "strict",
        "completed_within_budget", "invalid", "timeout", "failures",
    )
    table_rows = []
    ordered_groups = []
    for key in sorted(groups):
        group = groups[key]
        stats = _stats_payload(group)
        ordered_groups.append({"task": key[0], "agent": key[1], "mode": key[2], **stats})
        table_rows.append([
            group["task"],
            group["agent"],
            group["mode"],
            str(stats["attempted"]),
            str(stats["evaluation_valid"]),
            str(stats["strict_success"]),
            str(stats["completed_within_budget"]),
            str(stats["invalid"]),
            str(stats["timeout"]),
            _pair_text(stats["failure_pairs"]),
        ])
    for line in _format_table(headers, table_rows):
        print(line)
    totals_payload = _stats_payload(totals, include_runs=False)
    print(
        "upstream: "
        f"attempted={totals_payload['attempted']} "
        f"valid={totals_payload['evaluation_valid']} "
        f"strict={totals_payload['strict_success']}"
    )
    print(
        "product: "
        f"attempted={totals_payload['attempted']} "
        f"completed_within_budget={totals_payload['completed_within_budget']} "
        f"invalid={totals_payload['invalid']} "
        f"unknown_valid={totals_payload['valid_unknown']} "
        f"timeout={totals_payload['timeout']}"
    )
    print("failures_by_stage: " + _pair_text(totals_payload["failure_stages"]))
    print("failures_by_class: " + _pair_text(totals_payload["failure_classes"]))
    print(
        f"mixed_config={str(mixed).lower()} "
        f"mixed_fields={', '.join(mixed_fields) if mixed_fields else 'none'}"
    )
    issue_count = sum(bool(record["issues"]) for record in records)
    if issue_count:
        print(f"runs_with_read_issues={issue_count}")
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "kind": "forgeflow.g0_product_report",
        "mixed_config": mixed,
        "mixed_fields": mixed_fields,
        "totals": totals_payload,
        "groups": ordered_groups,
        "runs": records,
    }
    if out is not None and not _write_payload(out, payload, paths, "report"):
        return 2
    return 1 if mixed else 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="experiments.py",
        description="P5 experiment planner, recorded-run analyzer, and G0 evidence reports",
    )
    commands = parser.add_subparsers(dest="command", required=True)
    plan = commands.add_parser("plan", help="write the frozen experiment command matrix")
    plan.add_argument("--out", type=Path, required=True, help="JSON plan destination")
    plan.add_argument(
        "--repeats", type=_repeat_count, default=DEFAULT_REPEATS,
        help=f"Pi repeats per task/profile cell ({MIN_REPEATS}-{MAX_REPEATS})",
    )
    plan.add_argument("--execute", action="store_true", help="print commands without spawning them")
    analyze = commands.add_parser("analyze", help="group recorded runs without mixing profiles")
    analyze.add_argument("--runs", type=Path, default=Path("runs"), help="runs directory")
    analyze.add_argument("--json", dest="as_json", action="store_true", help="emit JSON groups")
    manifest = commands.add_parser(
        "manifest", help="write versioned configuration fingerprints for recorded runs"
    )
    manifest.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR")
    manifest.add_argument("--json", dest="json_out", type=Path, help="JSON destination")
    report = commands.add_parser(
        "report", help="report upstream and product metrics for recorded runs"
    )
    report.add_argument("run_dirs", nargs="+", type=Path, metavar="RUN_DIR")
    report.add_argument("--json", dest="json_out", type=Path, help="JSON destination")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "plan":
        return _command_plan(args.out, args.repeats, args.execute)
    if args.command == "analyze":
        return _command_analyze(args.runs, args.as_json)
    if args.command == "manifest":
        return _command_manifest(args.run_dirs, args.json_out)
    return _command_report(args.run_dirs, args.json_out)


if __name__ == "__main__":
    raise SystemExit(main())

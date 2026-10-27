"""Versioned, read-only RunView projection shared by the console and the product UI.

A RunView restates what a recorded run already contains: its experiment
configuration, the original evaluation, the controlled recovery artifacts and
its measured usage. It never starts a task, calls a model, touches the ERP, or
rewrites an original result.

Two rules keep the projection honest:

* A missing measurement stays ``None``. ``missing`` names the fields that were
  not recorded, so a reader cannot mistake "not measured" for zero or for pass.
* Business outcome (original verifier score and rules) and tool completion
  (prepare/execute artifacts) live in separate sections. A validated execution
  is not a business success, and a business pass does not prove the controlled
  executor ran.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime
from pathlib import Path
from typing import Any

from .agents.pi_usage import accounting_view
from .config import EXECUTION_GUIDANCE_PROFILES
from .recovery_evidence import execution_projection
from .runner import PLANNER_PROFILES, rule_projection, summarize_run

SCHEMA_VERSION = 1
KIND = "forgeflow.run_view"
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}")
PLAN_ID = re.compile(r"[a-f0-9]{64}")
MAX_JSON_BYTES = 4 * 1024 * 1024
MAX_INT = 2**53 - 1

TASK_LABELS = {
    "2280_medium_repair_plan_medium": "供应商断供",
    "2299_hard_repair_plan_hard": "产线停机",
}
DATA_PROFILE_LABELS = {
    "original": "原始任务",
    "planning-metadata-v1": "规划参数可见性变体",
    "unknown": "数据类型未识别",
}
GUIDANCE_LABELS = {
    "none": "未追加执行提示",
    "odoo-read-v1": "Odoo 接口执行提示",
    "odoo-schedule-v1": "Odoo 排期与回读提示",
    "recovery-workflow-v1": "恢复工作流提示",
    "unknown": "提示类型未识别",
}
PLANNER_LABELS = {
    "none": "未配置额外求解工具",
    "cp-sat-live-v1": "CP-SAT 命令行入口",
    "cp-sat-native-v1": "CP-SAT Pi 原生工具入口",
    "cp-sat-native-v2": "CP-SAT 原生工具 · 可修正输入反馈",
    "cp-sat-recovery-v1": "CP-SAT 变更集执行与回读",
    "unknown": "求解工具配置未识别",
}


def _local_file(path: Path, boundary: Path) -> bool:
    """Accept only regular files inside ``boundary`` with no symlinked component."""
    try:
        parts = path.relative_to(boundary).parts
    except ValueError:
        return False
    cursor = boundary
    if cursor.is_symlink():
        return False
    for part in parts:
        cursor /= part
        if cursor.is_symlink():
            return False
    return path.is_file() and path.resolve().is_relative_to(boundary.resolve())


def _json_file(path: Path, boundary: Path) -> dict[str, Any] | None:
    if not _local_file(path, boundary):
        return None
    try:
        if path.stat().st_size > MAX_JSON_BYTES:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _text(value: object, limit: int = 200) -> str | None:
    return value[:limit] if isinstance(value, str) else None


def _number(value: object) -> float | int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value if math.isfinite(value) else None


def _count(value: object) -> int | None:
    return value if type(value) is int and 0 <= value <= MAX_INT else None


def _boolean(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _elapsed(started: object, finished: object) -> float | None:
    if not isinstance(started, str) or not isinstance(finished, str):
        return None
    try:
        duration = (
            datetime.fromisoformat(finished) - datetime.fromisoformat(started)
        ).total_seconds()
        return round(duration, 1) if duration >= 0 else None
    except (TypeError, ValueError):
        return None


def experiment_labels(manifest: dict[str, Any]) -> dict[str, Any]:
    """Project known experiment labels, without exposing arbitrary prompt fields."""
    data_recorded = "data_profile" in manifest
    data = manifest.get("data_profile")
    data_name = data.get("name") if isinstance(data, dict) else None
    if not data_recorded:
        data_name = "original"
    elif data_name not in ("original", "planning-metadata-v1"):
        data_name = "unknown"
    guidance_recorded = "execution_guidance" in manifest
    guidance = manifest.get("execution_guidance")
    guidance = guidance if isinstance(guidance, dict) else {}
    guidance_name = guidance.get("profile")
    if not guidance_recorded:
        guidance_name = "none"
    elif guidance_name not in EXECUTION_GUIDANCE_PROFILES:
        guidance_name = "unknown"
    digest = guidance.get("sha256")
    digest = digest if isinstance(digest, str) and re.fullmatch(r"[0-9a-f]{64}", digest) else None
    version = guidance.get("version")
    version = version if type(version) is int and 0 < version <= MAX_INT else None
    planner_recorded = "planner" in manifest
    planner = manifest.get("planner")
    planner_name = planner.get("profile") if isinstance(planner, dict) else None
    if not planner_recorded:
        planner_name = "none"
    elif planner_name not in PLANNER_PROFILES:
        planner_name = "unknown"
    contract_guidance = manifest.get("contract_guidance")
    contract_guidance = contract_guidance if isinstance(contract_guidance, dict) else {}
    contract_profile = contract_guidance.get("profile", "none")
    if contract_profile not in ("none", "reviewed-request-v1"):
        contract_profile = "unknown"
    contract_digest = contract_guidance.get("sha256")
    contract_digest = contract_digest if isinstance(contract_digest, str) and re.fullmatch(
        r"[0-9a-f]{64}", contract_digest
    ) else None
    return {
        "data_profile": {
            "name": data_name,
            "label": DATA_PROFILE_LABELS[data_name],
            "recorded": data_recorded,
        },
        "planner": {
            "profile": planner_name,
            "label": PLANNER_LABELS[planner_name],
            "recorded": planner_recorded,
        },
        "execution_guidance": {
            "profile": guidance_name,
            "label": GUIDANCE_LABELS[guidance_name],
            "version": version if guidance_name not in ("none", "unknown") else None,
            "sha256": digest if guidance_name not in ("none", "unknown") else None,
            "recorded": guidance_recorded,
        },
        "contract_guidance": {
            "profile": contract_profile,
            "sha256": contract_digest if contract_profile == "reviewed-request-v1" else None,
            "recorded": "contract_guidance" in manifest,
        },
    }


def trial_directory(run_dir: Path) -> Path | None:
    """Return the single recorded Harbor trial, or None when it is ambiguous."""
    job_root = run_dir / "harbor/job"
    if job_root.is_symlink() or not job_root.is_dir():
        return None
    candidates = [
        path for path in sorted(job_root.glob("*"))
        if path.is_dir() and not path.is_symlink()
        and ((path / "config.json").is_file() or (path / "result.json").is_file()
             or (path / "verifier").is_dir())
    ]
    return candidates[0] if len(candidates) == 1 else None


def _relative(path: Path, runs_root: Path) -> str | None:
    try:
        return str(path.relative_to(runs_root.parent))
    except ValueError:
        return None


def _operation_counts(prepared: dict[str, Any] | None) -> dict[str, int | None] | None:
    counts = prepared.get("operation_counts") if isinstance(prepared, dict) else None
    if not isinstance(counts, dict):
        return None
    return {name: _count(counts.get(name)) for name in ("keep", "update", "cancel", "create")}


def _audited_writes(run_dir: Path, runs_root: Path) -> tuple[int | None, str | None]:
    """Read a supplemental audit's write count when it names this run exactly.

    The audit files are produced by separate read-only reviews and are not part
    of every run, so their absence must stay ``None`` rather than zero.
    """
    path = run_dir / "supplemental-evidence/execution-analysis.json"
    payload = _json_file(path, runs_root)
    if not isinstance(payload, dict) or payload.get("run_id") != run_dir.name:
        return None, None
    execution = payload.get("execution")
    writes = execution.get("writes") if isinstance(execution, dict) else None
    if isinstance(writes, list) and all(isinstance(row, dict) for row in writes):
        return len(writes), _relative(path, runs_root)
    business = payload.get("business_execution")
    count = business.get("mutating_http_count") if isinstance(business, dict) else None
    if _count(count) is not None:
        return count, _relative(path, runs_root)
    return None, None


def recovery_view(run_dir: Path, trial: Path | None, runs_root: Path) -> dict[str, Any]:
    """Describe prepare/execute artifacts without treating them as business success."""
    view: dict[str, Any] = {
        "status": "unknown",
        "plan_id": None,
        "operation_counts": None,
        "planned_step_count": None,
        "verified_steps": None,
        "execution_validated": None,
        "execution": None,
        "odoo_writes_planned": None,
        "odoo_writes_executed": None,
        "expected_new_spend": None,
        "audited_erp_writes": None,
        "audit_source": None,
        "consistent": None,
        "evidence": {"prepared": None, "success": None, "journal": None},
        "note": None,
    }
    if trial is None:
        view["note"] = "trial_directory_not_identified"
        return view
    directory = trial / "agent/recovery"
    if directory.is_symlink() or not directory.is_dir():
        view["status"] = "not_used"
        view["note"] = "no_recovery_artifacts_recorded"
        return view
    plan_dirs = []
    try:
        plan_dirs = [
            path for path in directory.iterdir()
            if path.is_dir() and not path.is_symlink() and PLAN_ID.fullmatch(path.name)
        ]
    except OSError:
        view["note"] = "recovery_directory_unreadable"
        return view
    if len(plan_dirs) != 1:
        view["note"] = "expected_exactly_one_plan_directory"
        return view
    plan_dir = plan_dirs[0]
    prepared_path = plan_dir / "prepared.json"
    success_path = plan_dir / "success.json"
    prepared = _json_file(prepared_path, runs_root)
    success = _json_file(success_path, runs_root)
    if prepared is None and success is None:
        view["note"] = "plan_artifacts_unreadable"
        return view

    plan_ids = {plan_dir.name}
    for payload in (prepared, success):
        value = payload.get("plan_id") if isinstance(payload, dict) else None
        if isinstance(value, str) and PLAN_ID.fullmatch(value):
            plan_ids.add(value)
    view["plan_id"] = plan_dir.name if len(plan_ids) <= 1 else None
    view["consistent"] = len(plan_ids) == 1

    execution = execution_projection(
        plan_dir, runs_root, prepared, success,
        success_present=success_path.exists() or success_path.is_symlink(),
    )
    if not view["consistent"]:
        execution = {**execution, "status": "unknown", "execution_validated": None,
                     "reason": "plan_ids_disagree"}
    view["execution"] = execution
    view["execution_validated"] = execution["execution_validated"]
    view["status"] = (
        "executed" if execution["status"] == "succeeded"
        else "prepared" if execution["status"] == "not_recorded" and isinstance(prepared, dict)
        else execution["status"] if execution["status"] in {"unknown", "stopped"}
        else "unknown"
    )
    if isinstance(prepared, dict):
        view["operation_counts"] = _operation_counts(prepared)
        view["planned_step_count"] = _count(prepared.get("step_count"))
        view["odoo_writes_planned"] = _boolean(prepared.get("odoo_writes"))
        view["expected_new_spend"] = _number(prepared.get("expected_new_spend"))
        if _local_file(prepared_path, runs_root):
            view["evidence"]["prepared"] = _relative(prepared_path, runs_root)
    if isinstance(success, dict):
        view["verified_steps"] = _count(success.get("verified_steps"))
        view["odoo_writes_executed"] = _boolean(success.get("odoo_writes"))
        if _local_file(success_path, runs_root):
            view["evidence"]["success"] = _relative(success_path, runs_root)
    journal = plan_dir / "journal.jsonl"
    if _local_file(journal, runs_root):
        view["evidence"]["journal"] = _relative(journal, runs_root)
    audited, source = _audited_writes(run_dir, runs_root)
    view["audited_erp_writes"] = audited
    view["audit_source"] = source
    return view


def usage_view(summary: dict[str, Any]) -> dict[str, Any]:
    """Keep recorded token use; never invent a cost or a total."""
    metrics = summary.get("metrics")
    metrics = metrics if isinstance(metrics, dict) else {}
    input_tokens = _count(metrics.get("n_input_tokens"))
    output_tokens = _count(metrics.get("n_output_tokens"))
    cache_tokens = _count(metrics.get("n_cache_tokens"))
    cost = _number(metrics.get("cost_usd"))
    accounting = accounting_view(metrics.get("usage_accounting"))
    total = (
        input_tokens + output_tokens
        if input_tokens is not None and output_tokens is not None
        else None
    )
    return {
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cache_tokens": cache_tokens,
        "total_tokens": total,
        "accounting": accounting,
        "total_tokens_note": (
            "已记录的输入与输出之和；缓存读写 token 已包含在输入内。"
            + {"complete": "普通调用与压缩用量已完整记录。",
               "incomplete": "记录不完整，不能作为完整模型用量。",
               "unknown": "旧记录未标记完整性，不能确认包含全部压缩用量。"}
            [accounting["status"]]
        ),
        "cost": {
            "usd": cost,
            "source": "recorded" if cost is not None else "not_reported",
            "completeness": accounting["cost_status"],
            "estimate": None,
            "estimate_basis": None,
        },
    }


def _rule_results(reward: dict[str, Any] | None) -> list[dict[str, Any]] | None:
    """Expose recorded outcomes without verifier expressions or arguments."""
    if rule_projection(reward) is None:
        return None

    def identifier(value: Any) -> str | None:
        return value if isinstance(value, str) and re.fullmatch(
            r"[A-Za-z][A-Za-z0-9_.-]{0,119}", value
        ) else None

    return [
        {
            "dimension": identifier(dimension),
            "rule": identifier(row.get("rule")),
            "status": row["status"],
            "applicable": row.get("applicable", row["status"] != "NA"),
        }
        for dimension, rows in reward["rules"]["by_dimension"].items()
        for row in rows
    ]


def _evaluation(
    summary: dict[str, Any], reward: dict[str, Any] | None
) -> tuple[dict[str, Any], list[str]]:
    projection = rule_projection(reward)
    all_issues = summary.get("issues") or []
    issues = [str(issue)[:300] for issue in all_issues][:20]
    view = {
        "score": _number(summary.get("score")),
        "score_scale": summary.get("score_scale"),
        "original_passed": _boolean(summary.get("original_passed")),
        "raw_task_strict_success": _boolean(summary.get("raw_task_strict_success")),
        "strict_success": _boolean(summary.get("strict_success")),
        "evaluation_valid": _boolean(summary.get("evaluation_valid")),
        "environment_chain_ok": _boolean(summary.get("environment_chain_ok")),
        "expected_control_outcome": _boolean(summary.get("expected_control_outcome")),
        "applicable_rule_count": projection["applicable"] if projection else None,
        "passed_rule_count": projection["passed"] if projection else None,
        "failed_rule_count": _count(summary.get("failed_rule_count")),
        "all_applicable_rules_pass": _boolean(summary.get("all_applicable_rules_pass")),
        "rule_results": _rule_results(reward),
        "error_type": _error_type(summary),
        "issue_count": len(all_issues),
        "issues": issues,
    }
    inconsistencies = []
    if projection is not None and view["failed_rule_count"] is not None:
        if view["failed_rule_count"] != projection["failed"]:
            inconsistencies.append("rule_counts_disagree_between_reward_and_summary")
    return view, inconsistencies


def _error_type(summary: dict[str, Any]) -> str | None:
    error = summary.get("trial_exception") or summary.get("runner_error")
    error = error if isinstance(error, dict) else {}
    value = error.get("exception_type") or error.get("type")
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_.]{0,119}", value):
        return None
    return value


def build_run_view(run_dir: str | Path, runs_root: str | Path) -> dict[str, Any] | None:
    """Project one recorded run, or None when it is not a readable local run."""
    run_dir = Path(run_dir)
    runs_root = Path(runs_root)
    if run_dir.is_symlink() or not RUN_ID.fullmatch(run_dir.name):
        return None
    manifest = _json_file(run_dir / "manifest.json", runs_root)
    if manifest is None:
        return None
    # Reject indirect evidence paths before delegating to the read-only summarizer.
    for path in run_dir.rglob("*"):
        if path.is_symlink():
            return None
    try:
        summary = summarize_run(run_dir)
    except (OSError, ValueError, TypeError, OverflowError):
        return None
    model = manifest.get("model")
    model = model if isinstance(model, dict) else {}
    budget = manifest.get("budget")
    budget = budget if isinstance(budget, dict) else {}
    sampling = budget.get("sampling_params")
    sampling = sampling if isinstance(sampling, dict) else {}
    agent = summary.get("agent")
    task = _text(summary.get("task"), 120)
    reward_path = summary.get("raw_reward_path")
    reward = (
        _json_file(run_dir / reward_path, runs_root)
        if isinstance(reward_path, str) and reward_path else None
    )
    evaluation, inconsistencies = _evaluation(summary, reward)
    trial = trial_directory(run_dir)
    recovery = recovery_view(run_dir, trial, runs_root)
    if recovery["consistent"] is False:
        inconsistencies.append("recovery_plan_ids_disagree")
    started_at = _text(manifest.get("started_at_utc"), 50)
    finished_at = _text(manifest.get("finished_at_utc"), 50)
    usage = usage_view(summary)
    security = manifest.get("security_profile")
    security = security if isinstance(security, dict) else {}
    security_name = security.get("name")
    security_name = security_name if security_name in ("none", "verifier-hardened-v1",
                                                        "permission-split-v1") else "none"
    added = security.get("verifier_env_added")
    added = sorted(added) if isinstance(added, dict) else []
    view = {
        "schema_version": SCHEMA_VERSION,
        "kind": KIND,
        "run_id": run_dir.name,
        "task": {
            "id": task,
            "label": TASK_LABELS.get(task, "其他任务"),
        },
        "agent": {
            "name": agent if agent in ("pi", "oracle", "nop") else "unknown",
            "category": (
                "agent_evaluation" if agent == "pi"
                else "environment_control" if agent in ("oracle", "nop")
                else "unclassified"
            ),
        },
        "state": _text(summary.get("state"), 40),
        "mode": _text(summary.get("mode"), 80),
        "timing": {
            "started_at": started_at,
            "finished_at": finished_at,
            "elapsed_seconds": _elapsed(manifest.get("started_at_utc"),
                                        manifest.get("finished_at_utc")),
        },
        "experiment": {
            "model": _text(model.get("model")),
            "budget_seconds": _number(budget.get("agent_timeout_sec")),
            "thinking_mode": (
                manifest.get("thinking_mode")
                if agent == "pi" and manifest.get("thinking_mode") in ("provider-default", "off")
                else None
            ),
            "context_window_tokens": _number(budget.get("context_window_tokens")),
            "sampling_params": {
                key: _number(sampling.get(key)) for key in ("temperature", "top_p")
            },
            "security_profile": {
                "name": security_name,
                "recorded": "security_profile" in manifest,
                "verifier_env_added": added,
            },
            "output_policy": {
                "max_output_tokens": (
                    budget.get("max_output_tokens")
                    if type(budget.get("max_output_tokens")) is int
                    and 0 < budget.get("max_output_tokens") <= MAX_INT
                    else None
                ),
                "source": (
                    budget.get("max_output_tokens_source")
                    if budget.get("max_output_tokens_source") in (
                        "provider_default", "adapter_default", "explicit_run_argument",
                    )
                    else None
                ),
                "version": (
                    budget.get("output_policy_version")
                    if budget.get("output_policy_version") in ("all-requests-v2", "explicit")
                    else None
                ),
                "request_fields": (
                    budget.get("output_limit_request_fields")
                    if budget.get("output_limit_request_fields") in ("omitted", "included")
                    else None
                ),
            },
            **experiment_labels(manifest),
        },
        "evaluation": evaluation,
        "recovery": recovery,
        "usage": usage,
        "evidence": {
            "manifest": _relative(run_dir / "manifest.json", runs_root),
            "reward": (
                f"runs/{run_dir.name}/{reward_path}" if isinstance(reward_path, str) else None
            ),
            "result": (
                f"runs/{run_dir.name}/{summary['raw_result_path']}"
                if isinstance(summary.get("raw_result_path"), str) else None
            ),
        },
        "consistency": {"ok": not inconsistencies, "issues": inconsistencies},
        "attestation": attestation_view(run_dir),
    }
    view["missing"] = _missing(view)
    return view


def attestation_view(run_dir: Path) -> dict[str, Any]:
    """Report whether the verifier artifacts carry a valid signed record.

    An old run has no record and is reported as ``absent``; a present record
    that fails verification is reported as ``invalid`` with a stable reason.
    Nothing here changes a score or an evaluation flag.
    """
    directory = run_dir / "attestation"
    if directory.is_symlink() or not directory.is_dir():
        return {"status": "absent", "reason": "record_missing"}
    record_path = directory / "verifier.json"
    if record_path.is_symlink() or not record_path.is_file():
        return {"status": "absent", "reason": "record_missing"}
    try:
        from .attestation import load_record
        loaded = load_record(run_dir)
    except Exception:
        return {"status": "invalid", "reason": "record_unreadable"}
    if loaded.get("verified") is True:
        return {"status": "verified", "reason": None}
    return {"status": "invalid", "reason": loaded.get("reason") or "signature_mismatch"}


def _missing(view: dict[str, Any]) -> list[str]:
    """Name the fields a reader must treat as unmeasured, not as zero."""
    paths: list[str] = []
    evaluation = view["evaluation"]
    for key in ("score", "strict_success", "evaluation_valid"):
        if evaluation[key] is None:
            paths.append(f"evaluation.{key}")
    if evaluation["applicable_rule_count"] is None:
        paths.append("evaluation.applicable_rule_count")
    usage = view["usage"]
    for key in ("input_tokens", "output_tokens", "cache_tokens", "total_tokens"):
        if usage[key] is None:
            paths.append(f"usage.{key}")
    if usage["cost"]["usd"] is None:
        paths.append("usage.cost.usd")
    recovery = view["recovery"]
    if recovery["status"] in ("unknown",):
        paths.append("recovery.status")
    if recovery["audited_erp_writes"] is None:
        paths.append("recovery.audited_erp_writes")
    if view["timing"]["elapsed_seconds"] is None:
        paths.append("timing.elapsed_seconds")
    return paths


def legacy_record(view: dict[str, Any]) -> dict[str, Any]:
    """Flatten a RunView into the field names the existing console already serves."""
    experiment = view["experiment"]
    evaluation = view["evaluation"]
    usage = view["usage"]
    output = experiment["output_policy"]
    return {
        "data_profile": experiment["data_profile"]["name"],
        "data_profile_label": experiment["data_profile"]["label"],
        "data_profile_recorded": experiment["data_profile"]["recorded"],
        "planner_profile": experiment["planner"]["profile"],
        "planner_label": experiment["planner"]["label"],
        "planner_recorded": experiment["planner"]["recorded"],
        "execution_guidance": dict(experiment["execution_guidance"]),
        "run_id": view["run_id"],
        "task": view["task"]["id"],
        "task_label": view["task"]["label"],
        "agent": view["agent"]["name"],
        "category": view["agent"]["category"],
        "state": view["state"],
        "started_at": view["timing"]["started_at"],
        "finished_at": view["timing"]["finished_at"],
        "elapsed_seconds": view["timing"]["elapsed_seconds"],
        "model": experiment["model"],
        "budget_seconds": experiment["budget_seconds"],
        "max_output_tokens": output["max_output_tokens"],
        "max_output_tokens_source": output["source"],
        "output_policy_version": output["version"],
        "thinking_mode": experiment["thinking_mode"],
        "security_profile": experiment["security_profile"]["name"],
        "security_profile_recorded": experiment["security_profile"]["recorded"],
        "security_profile_verifier_env": list(
            experiment["security_profile"]["verifier_env_added"]),
        "context_window_tokens": experiment["context_window_tokens"],
        "sampling_params": dict(experiment["sampling_params"]),
        "score": evaluation["score"],
        "original_passed": evaluation["original_passed"],
        "strict_success": evaluation["strict_success"],
        "evaluation_valid": evaluation["evaluation_valid"],
        "expected_control_outcome": evaluation["expected_control_outcome"],
        "all_applicable_rules_pass": evaluation["all_applicable_rules_pass"],
        "failed_rule_count": evaluation["failed_rule_count"],
        "error_type": evaluation["error_type"],
        "issue_count": evaluation["issue_count"],
        "metrics": {
            "n_input_tokens": usage["input_tokens"],
            "n_output_tokens": usage["output_tokens"],
            "n_cache_tokens": usage["cache_tokens"],
            "cost_usd": usage["cost"]["usd"],
            "usage_accounting": usage["accounting"],
        },
        "attestation": view["attestation"]["status"],
        "evidence": {
            "manifest": view["evidence"]["manifest"],
            "reward": view["evidence"]["reward"],
            "result": view["evidence"]["result"],
        },
    }

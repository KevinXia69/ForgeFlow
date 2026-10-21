"""Tests for the offline P5 experiment planner and run analyzer."""

import hashlib
import importlib.util
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
TASK_A = "2280_medium_repair_plan_medium"
TASK_B = "2299_hard_repair_plan_hard"
MODEL = "nvidia/Nemotron-3.5-Super-VL"


def _load_experiments():
    spec = importlib.util.spec_from_file_location(
        "p5_experiments", ROOT / "scripts/experiments.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


experiments = _load_experiments()


def _write_run(
    runs: Path,
    run_id: str,
    *,
    task: str = TASK_A,
    agent: str = "pi",
    security: str = "none",
    data: str = "planning-metadata-v1",
    planner: str = "cp-sat-recovery-v1",
    model: str | None = MODEL,
    score: float | None = 100.0,
    valid: bool = True,
    chain_ok: bool = True,
    strict: bool | None = True,
) -> Path:
    directory = runs / run_id
    directory.mkdir()
    manifest = {
        "run_id": run_id,
        "task": task,
        "agent": agent,
        "security_profile": {"name": security},
        "data_profile": {"name": data},
        "planner": {"profile": planner},
        "model": {"model": model} if model is not None else None,
        "state": "completed",
    }
    summary = {
        "run_id": run_id,
        "task": task,
        "agent": agent,
        "data_profile": data,
        "score": score,
        "evaluation_valid": valid,
        "environment_chain_ok": chain_ok,
        "strict_success": strict,
        "state": "completed",
    }
    for name, payload in (("manifest.json", manifest), ("summary.json", summary)):
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    return directory


def test_plan_writes_repeats_and_execute_never_spawns(tmp_path, capsys, monkeypatch):
    monkeypatch.chdir(tmp_path)
    out = tmp_path / "plan.json"
    assert experiments.main(["plan", "--out", str(out), "--repeats", "2"]) == 0
    plan = json.loads(out.read_text(encoding="utf-8"))
    entries = plan["entries"]
    assert plan["repeats"] == 2
    assert len({entry["id"] for entry in entries}) == len(entries)
    assert len({entry["run_id"] for entry in entries}) == len(entries)

    pi_entries = [entry for entry in entries if entry["agent"] == "pi"]
    assert len(pi_entries) == 8
    for task in ("A", "B"):
        for security in ("none", "permission-split-v1"):
            cell = [
                entry
                for entry in pi_entries
                if entry["task"] == task and entry["security_profile"] == security
            ]
            assert len(cell) == 2
            assert {entry["run_id"] for entry in cell} == {
                f"p5-{task}-{security}-pi-r1",
                f"p5-{task}-{security}-pi-r2",
            }
    controls = [entry for entry in entries if entry["agent"] == "nop"]
    assert len(controls) == 4
    assert {entry["task"] for entry in controls} == {"A", "B"}
    for entry in entries:
        assert "--security-profile" in entry["command"]
        assert "--data-profile" in entry["command"]
        assert entry["run_id"] in entry["command"]
        assert entry["command"][:5] == ["uv", "run", "forgeflow", "run", entry["task"]]

    capsys.readouterr()
    execute_out = tmp_path / "execute-plan.json"
    assert (
        experiments.main(
            ["plan", "--out", str(execute_out), "--repeats", "1", "--execute"]
        )
        == 0
    )
    printed = capsys.readouterr().out
    assert "uv run forgeflow run A --agent pi" in printed
    assert "p5-A-none-pi-r1" in printed
    assert not (tmp_path / "runs").exists()
    assert not any(path.name.startswith("p5-") for path in tmp_path.iterdir())


@pytest.mark.parametrize("repeats", [0, 11])
def test_plan_rejects_repeats_outside_one_to_ten(tmp_path, repeats):
    out = tmp_path / "plan.json"
    with pytest.raises(SystemExit) as error:
        experiments.main(["plan", "--out", str(out), "--repeats", str(repeats)])
    assert error.value.code == 2
    assert not out.exists()


def test_analyze_groups_scores_and_json(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "a-1", score=100.0, strict=True)
    _write_run(runs, "a-2", score=80.0, strict=False)
    _write_run(runs, "a-3", score=60.0, strict=False)
    _write_run(runs, "a-invalid", score=None, valid=False, strict=None)
    _write_run(runs, "b-1", task=TASK_B, score=50.0, strict=False)
    broken = runs / "broken"
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    (broken / "summary.json").write_text("{}", encoding="utf-8")

    assert experiments.main(["analyze", "--runs", str(runs)]) == 0
    output = capsys.readouterr().out
    lines = output.splitlines()
    group_a = next(line for line in lines if line.startswith(TASK_A))
    group_b = next(line for line in lines if line.startswith(TASK_B))
    assert group_a.split()[4:10] == ["4", "3", "80.00", "60.00", "100.00", "1"]
    assert group_b.split()[4:9] == ["1", "1", "50.00", "insufficient", "insufficient"]
    assert "mixed_config=false" in output
    assert "skipped=1" in output

    assert experiments.main(["analyze", "--runs", str(runs), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["skipped"] == 1
    assert len(payload["groups"]) == 2
    group = next(item for item in payload["groups"] if item["task"] == TASK_A)
    assert group["security_profile"] == "none"
    assert group["data_profile"] == "planning-metadata-v1"
    assert group["agent"] == "pi"
    assert group["runs"] == 4
    assert group["valid"] == 3
    assert group["score_mean"] == pytest.approx(80.0)
    assert group["score_min"] == 60.0
    assert group["score_max"] == 100.0
    assert group["strict_pass"] == 1
    single = next(item for item in payload["groups"] if item["task"] == TASK_B)
    assert single["score_mean"] == 50.0
    assert single["score_min"] is None
    assert single["score_max"] is None


def test_mixed_models_flag_and_exit_one(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "m-1", model="model-a")
    _write_run(runs, "m-2", model="model-b")

    assert experiments.main(["analyze", "--runs", str(runs), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["mixed_config"] is True
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["mixed_config"] is True
    assert group["models"] == ["model-a", "model-b"]
    assert group["runs"] == 2


def test_mixed_planner_profiles_flag_and_exit_one(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "p-1", planner="none")
    _write_run(runs, "p-2", planner="cp-sat-recovery-v1")

    assert experiments.main(["analyze", "--runs", str(runs), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    group = payload["groups"][0]
    assert group["mixed_config"] is True
    assert group["planner_profiles"] == ["cp-sat-recovery-v1", "none"]


def test_environment_chain_failure_exits_one(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "failed-env", score=None, valid=False, chain_ok=False, strict=None)

    assert experiments.main(["analyze", "--runs", str(runs), "--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["environment_chain_ok"] is False
    assert payload["mixed_config"] is False
    assert payload["groups"][0]["environment_chain_failed"] == 1


def test_security_profiles_never_share_a_group(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_run(runs, "plain", security="none")
    _write_run(runs, "split", security="permission-split-v1")

    assert experiments.main(["analyze", "--runs", str(runs), "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["groups"]) == 2
    assert {group["security_profile"] for group in payload["groups"]} == {
        "none",
        "permission-split-v1",
    }
    assert all(group["runs"] == 1 for group in payload["groups"])


def _write_g0_run(
    runs: Path,
    run_id: str,
    *,
    task: str = TASK_A,
    agent: str = "pi",
    model: str | None = MODEL,
    thinking: str | None = "provider-default",
    data: str | None = "planning-metadata-v1",
    planner: str | None = "cp-sat-recovery-v1",
    security: str | None = "permission-split-v1",
    guidance: str | None = "none",
    guidance_digest: str | None = None,
    budget: float | None = 600.0,
    elapsed: float | None = 100.0,
    state: str = "completed",
    valid: bool = True,
    strict: bool | None = True,
    chain_ok: bool | None = None,
    trial_exception: dict | None = None,
    score: float | None = None,
    tool_calls: int | None = 12,
    input_tokens: int | None = 100,
    output_tokens: int | None = 10,
    cache_tokens: int | None = 20,
    cost_usd: float | None = None,
    own_commit: str | None = "a" * 40,
    upstream_commit: str | None = "b" * 40,
    image_ref: str | None = "odoo:19",
    image_digest: str | None = "c" * 64,
    contract_digest: str | None = "d" * 64,
    verifier_digest: str | None = "e" * 64,
    implementation_digest: str | None = "8" * 64,
    anchor: str | None = "2026-02-03",
    snapshot: str | None = "f" * 64,
) -> Path:
    directory = runs / run_id
    directory.mkdir()
    manifest: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "task": task,
        "agent": agent,
        "state": state,
        "mode": "benchmark",
    }
    if thinking is not None:
        manifest["thinking_mode"] = thinking
    if model is not None:
        manifest["model"] = {"model": model, "provider": "nebius"}
    if data is not None:
        manifest["data_profile"] = {
            "name": data,
            "execution_sha256": "5" * 64,
            "diff_sha256": "6" * 64,
            "source_sha256": "7" * 64,
        }
    if planner is not None:
        manifest["planner"] = {"profile": planner, "enabled": True}
    if security is not None:
        manifest["security_profile"] = {
            "name": security,
            "verifier_env_added": {"PYTHONSAFEPATH": "1"},
        }
    if guidance is not None:
        digest = guidance_digest
        if digest is None and guidance != "none":
            digest = "9" * 64
        manifest["execution_guidance"] = {
            "profile": guidance,
            "sha256": digest,
            "version": 1 if guidance != "none" else None,
        }
    if budget is not None:
        manifest["budget"] = {
            "agent_timeout_sec": budget,
            "agent_setup_timeout_sec": 1800.0,
            "attempts": 1,
            "concurrency": 1,
            "max_retries": 0,
            "max_output_tokens": None,
            "max_output_tokens_source": "provider_default",
            "output_policy_version": "all-requests-v2",
            "output_limit_request_fields": "omitted",
            "output_budget_scope": "Per model response.",
            "context_window_tokens": 131072,
            "sampling_params": {"temperature": 1.0, "top_p": 0.95},
        }
    if elapsed is not None:
        started = datetime(2026, 1, 1, tzinfo=timezone.utc)
        finished = started + timedelta(seconds=elapsed)
        manifest["started_at_utc"] = started.isoformat()
        manifest["finished_at_utc"] = finished.isoformat()
    if own_commit is not None:
        manifest["commit"] = own_commit
    if upstream_commit is not None:
        manifest["upstream"] = {
            "erp-bench": {"commit": upstream_commit, "archive_sha256": "1" * 64},
            "pi-mono": {"commit": "2" * 40, "archive_sha256": "3" * 64},
        }
    if implementation_digest is not None:
        manifest["implementation_snapshot"] = {"archive_sha256": implementation_digest}
    if image_ref is not None or image_digest is not None:
        manifest["image"] = {"ref": image_ref, "digest": image_digest}
    if contract_digest is not None:
        manifest["contract"] = {
            "sha256": contract_digest,
            "source": "contracts/test.json",
            "enabled": True,
        }
    if verifier_digest is not None:
        manifest["task_derivation"] = {
            "original_file_sha256": {
                "tests/checks.py": verifier_digest,
                "tests/test.sh": "4" * 64,
            }
        }
    manifest["verifier_adapter"] = "forgeflow.verifier:ERPBenchVerifier"
    if anchor is not None:
        manifest["business_anchor_date"] = anchor
    if snapshot is not None:
        manifest["start_snapshot_sha256"] = snapshot
    metrics: dict[str, object] = {
        "n_input_tokens": input_tokens,
        "n_output_tokens": output_tokens,
        "n_cache_tokens": cache_tokens,
        "cost_usd": cost_usd,
    }
    if tool_calls is not None:
        metrics["tool_calls"] = tool_calls
    summary: dict[str, object] = {
        "schema_version": 1,
        "run_id": run_id,
        "task": task,
        "agent": agent,
        "mode": "benchmark",
        "state": state,
        "evaluation_valid": valid,
        "environment_chain_ok": valid if chain_ok is None else chain_ok,
        "strict_success": strict,
        "score": score if score is not None else (100.0 if valid else 0.0),
        "score_scale": [0, 100],
        "issues": [],
        "metrics": metrics,
    }
    if trial_exception is not None:
        summary["trial_exception"] = trial_exception
    for name, payload in (("manifest.json", manifest), ("summary.json", summary)):
        (directory / name).write_text(
            json.dumps(payload, sort_keys=True), encoding="utf-8"
        )
    return directory


def _tree_state(roots: list[Path]) -> dict[str, tuple]:
    state: dict[str, tuple] = {}
    for root in roots:
        for path in sorted(root.rglob("*")):
            if path.is_symlink():
                state[str(path)] = ("symlink",)
            elif path.is_dir():
                state[str(path)] = ("dir",)
            elif path.is_file():
                state[str(path)] = (
                    "file",
                    path.stat().st_size,
                    hashlib.sha256(path.read_bytes()).hexdigest(),
                )
    return state


def test_manifest_fingerprint_same_config_not_mixed_and_read_only(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    first = _write_g0_run(runs, "g0-a-1")
    second = _write_g0_run(runs, "g0-a-2", elapsed=200, input_tokens=200)
    before = _tree_state([first, second])
    out = tmp_path / "fingerprints.json"

    assert experiments.main(
        ["manifest", str(first), str(second), "--json", str(out)]
    ) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 3
    assert payload["mixed_config"] is False
    assert payload["mixed_fields"] == []
    assert len(payload["runs"]) == 2
    record = next(item for item in payload["runs"] if item["run_id"] == "g0-a-1")
    fingerprint = record["fingerprint"]
    assert fingerprint["schema_version"] == 3
    assert fingerprint["source"]["commit"] == "a" * 40
    assert fingerprint["source"]["implementation_sha256"] == "8" * 64
    assert fingerprint["upstream"]["commits"]["erp-bench"] == "b" * 40
    assert len(fingerprint["upstream"]["lock_sha256"]) == 64
    assert fingerprint["environment"]["ref"] == "odoo:19"
    assert fingerprint["environment"]["digest_sha256"] == "c" * 64
    assert fingerprint["task"]["id"] == TASK_A
    assert len(fingerprint["task"]["input_sha256"]) == 64
    assert fingerprint["model"]["id"] == MODEL
    assert fingerprint["model"]["thinking_mode"] == "provider-default"
    assert fingerprint["budget"]["agent_timeout_sec"] == 600
    assert fingerprint["budget"]["max_output_tokens"] is None
    assert fingerprint["budget"]["max_output_tokens_source"] == "provider_default"
    assert fingerprint["profiles"]["data"]["name"] == "planning-metadata-v1"
    assert fingerprint["profiles"]["data"]["content_sha256"] == "5" * 64
    assert fingerprint["profiles"]["planner"]["name"] == "cp-sat-recovery-v1"
    assert fingerprint["profiles"]["security"]["name"] == "permission-split-v1"
    assert fingerprint["profiles"]["guidance"]["name"] == "none"
    assert fingerprint["contract"] == {
        "id": None,
        "sha256": "d" * 64,
        "source": "contracts/test.json",
        "enabled": True,
    }
    assert fingerprint["verifier"]["adapter"] == "forgeflow.verifier:ERPBenchVerifier"
    assert len(fingerprint["verifier"]["content_sha256"]) == 64
    assert fingerprint["approval"]["mode"] == "evaluation"
    assert fingerprint["business"]["anchor_date"] == "2026-02-03"
    assert fingerprint["business"]["start_snapshot_sha256"] == "f" * 64
    product = record["product"]
    assert product["attempted"] is True
    assert product["completed_within_budget"] is True
    assert product["tool_calls"] == 12
    assert product["input_tokens"] == 100
    assert product["output_tokens"] == 10
    assert product["cache_tokens"] == 20
    assert product["actual_cost"] is None
    assert "mixed_config=false" in capsys.readouterr().out
    assert _tree_state([first, second]) == before


def test_manifest_missing_fingerprint_values_stay_null(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    directory = runs / "g0-missing"
    directory.mkdir()
    manifest = {
        "schema_version": 1,
        "run_id": "g0-missing",
        "task": TASK_A,
        "agent": "pi",
        "state": "completed",
    }
    summary = {
        "schema_version": 1,
        "run_id": "g0-missing",
        "task": TASK_A,
        "agent": "pi",
        "state": "completed",
        "evaluation_valid": True,
        "strict_success": True,
        "score": 100.0,
        "metrics": {},
    }
    for name, payload in (("manifest.json", manifest), ("summary.json", summary)):
        (directory / name).write_text(json.dumps(payload), encoding="utf-8")
    out = tmp_path / "fingerprints.json"

    assert experiments.main(["manifest", str(directory), "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    fingerprint = payload["runs"][0]["fingerprint"]
    assert fingerprint["source"]["commit"] is None
    assert fingerprint["source"]["implementation_sha256"] is None
    assert fingerprint["upstream"]["lock_sha256"] is None
    assert fingerprint["upstream"]["commits"] is None
    assert fingerprint["environment"]["ref"] is None
    assert fingerprint["environment"]["digest_sha256"] is None
    assert fingerprint["task"]["input_sha256"] is None
    assert fingerprint["model"]["id"] is None
    assert fingerprint["model"]["thinking_mode"] is None
    assert fingerprint["budget"]["recorded"] is False
    assert fingerprint["budget"]["agent_timeout_sec"] is None
    assert fingerprint["profiles"]["data"]["recorded"] is False
    assert fingerprint["profiles"]["data"]["name"] is None
    assert fingerprint["profiles"]["planner"]["recorded"] is False
    assert fingerprint["profiles"]["planner"]["name"] is None
    assert fingerprint["profiles"]["security"]["recorded"] is False
    assert fingerprint["profiles"]["security"]["name"] is None
    assert fingerprint["profiles"]["guidance"]["recorded"] is False
    assert fingerprint["profiles"]["guidance"]["name"] is None
    assert fingerprint["contract"] == {
        "id": None,
        "sha256": None,
        "source": None,
        "enabled": None,
    }
    assert fingerprint["verifier"]["adapter"] is None
    assert fingerprint["verifier"]["content_sha256"] is None
    assert fingerprint["approval"]["mode"] is None
    assert fingerprint["business"]["anchor_date"] is None
    assert fingerprint["business"]["start_snapshot_sha256"] is None
    product = payload["runs"][0]["product"]
    assert product["completed_within_budget"] is None
    assert product["over_budget"] is None
    assert product["failure_stage"] is None
    assert product["tool_calls"] is None
    assert product["input_tokens"] is None
    assert product["actual_cost"] is None


def test_manifest_keeps_empty_run_dir_with_null_fingerprint(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    directory = runs / "g0-empty"
    directory.mkdir()
    out = tmp_path / "fingerprints.json"

    assert experiments.main(["manifest", str(directory), "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mixed_config"] is False
    record = payload["runs"][0]
    assert record["run_id"] == "g0-empty"
    assert record["issues"] == ["manifest.json: missing", "summary.json: missing"]
    assert record["fingerprint"]["task"]["id"] is None
    assert record["fingerprint"]["model"]["id"] is None
    assert record["product"]["attempted"] is True
    assert record["product"]["evaluation_valid"] is None


def test_mixed_config_compares_more_than_model_and_planner(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    first = _write_g0_run(runs, "g0-budget-600", budget=600)
    second = _write_g0_run(runs, "g0-budget-300", budget=300)
    out = tmp_path / "fingerprints.json"

    assert experiments.main(
        ["manifest", str(first), str(second), "--json", str(out)]
    ) == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["mixed_config"] is True
    assert payload["mixed_fields"] == ["budget.agent_timeout_sec"]
    assert payload["runs"][0]["fingerprint"]["model"]["id"] == MODEL
    assert payload["runs"][1]["fingerprint"]["model"]["id"] == MODEL
    assert payload["runs"][0]["fingerprint"]["profiles"]["planner"]["name"] == (
        "cp-sat-recovery-v1"
    )
    assert payload["runs"][1]["fingerprint"]["profiles"]["planner"]["name"] == (
        "cp-sat-recovery-v1"
    )


def test_contract_guidance_is_a_separate_safe_fingerprint_factor(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    first = _write_g0_run(runs, "no-guidance-record")
    second = _write_g0_run(runs, "reviewed-request")
    path = second / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["contract_guidance"] = {
        "profile": "reviewed-request-v1", "version": 1, "sha256": "e" * 64,
        "text": "prompt contents must not enter the report",
    }
    path.write_text(json.dumps(manifest))
    out = tmp_path / "fingerprints.json"
    assert experiments.main(["manifest", str(first), str(second), "--json", str(out)]) == 1
    payload = json.loads(out.read_text())
    assert all(field.startswith("profiles.contract_guidance.") for field in payload["mixed_fields"])
    assert payload["runs"][0]["fingerprint"]["profiles"]["contract_guidance"]["recorded"] is False
    assert "prompt contents" not in out.read_text()


def test_completed_within_budget_is_conservative(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_g0_run(runs, "g0-ok", elapsed=100, budget=600)
    _write_g0_run(runs, "g0-over", elapsed=700, budget=600)
    _write_g0_run(
        runs,
        "g0-timeout",
        elapsed=700,
        budget=600,
        state="failed",
        valid=False,
        strict=None,
        trial_exception={
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 600.0 seconds",
        },
    )
    _write_g0_run(runs, "g0-no-budget", elapsed=100, budget=None)
    _write_g0_run(runs, "g0-no-elapsed", elapsed=None, budget=600)
    _write_g0_run(
        runs,
        "g0-invalid",
        elapsed=100,
        budget=600,
        valid=False,
        strict=None,
        chain_ok=False,
    )
    out = tmp_path / "fingerprints.json"

    assert experiments.main(
        [
            "manifest",
            str(runs / "g0-ok"),
            str(runs / "g0-over"),
            str(runs / "g0-timeout"),
            str(runs / "g0-no-budget"),
            str(runs / "g0-no-elapsed"),
            str(runs / "g0-invalid"),
            "--json",
            str(out),
        ]
    ) == 1
    payload = json.loads(out.read_text(encoding="utf-8"))
    records = {item["run_id"]: item["product"] for item in payload["runs"]}
    assert records["g0-ok"]["completed_within_budget"] is True
    assert records["g0-ok"]["failure_stage"] is None
    assert records["g0-over"]["completed_within_budget"] is False
    assert records["g0-over"]["over_budget"] is True
    assert records["g0-over"]["failure_stage"] == "budget"
    assert records["g0-over"]["failure_class"] == "over_budget"
    assert records["g0-timeout"]["completed_within_budget"] is False
    assert records["g0-timeout"]["timeout"] is True
    assert records["g0-timeout"]["failure_stage"] == "agent"
    assert records["g0-timeout"]["failure_class"] == "timeout"
    assert records["g0-timeout"]["failure_error_type"] == "AgentTimeoutError"
    assert records["g0-no-budget"]["completed_within_budget"] is None
    assert records["g0-no-elapsed"]["completed_within_budget"] is None
    assert records["g0-invalid"]["completed_within_budget"] is False
    assert records["g0-invalid"]["failure_stage"] == "verifier"
    assert records["g0-invalid"]["failure_class"] == "evaluation_invalid"


def test_report_keeps_invalid_and_timeout_runs_in_denominator(tmp_path, capsys):
    runs = tmp_path / "runs"
    runs.mkdir()
    _write_g0_run(runs, "g0-report-ok", elapsed=100, budget=600)
    _write_g0_run(
        runs,
        "g0-report-timeout",
        elapsed=700,
        budget=600,
        state="failed",
        valid=False,
        strict=None,
        trial_exception={
            "exception_type": "AgentTimeoutError",
            "exception_message": "Agent execution timed out after 600.0 seconds",
        },
    )
    _write_g0_run(
        runs,
        "g0-report-invalid",
        elapsed=100,
        budget=600,
        valid=False,
        strict=None,
        chain_ok=False,
    )
    out = tmp_path / "report.json"

    assert experiments.main(
        [
            "report",
            str(runs / "g0-report-ok"),
            str(runs / "g0-report-timeout"),
            str(runs / "g0-report-invalid"),
            "--json",
            str(out),
        ]
    ) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    totals = payload["totals"]
    assert totals["attempted"] == 3
    assert totals["evaluation_valid"] == 1
    assert totals["invalid"] == 2
    assert totals["strict_success"] == 1
    assert totals["completed_within_budget"] == 1
    assert totals["timeout"] == 1
    assert totals["failures"] == 2
    assert totals["failure_stages"] == {"agent": 1, "verifier": 1}
    assert totals["failure_classes"] == {"evaluation_invalid": 1, "timeout": 1}
    assert totals["failure_pairs"] == {
        "agent:timeout": 1,
        "verifier:evaluation_invalid": 1,
    }
    assert len(payload["runs"]) == 3
    assert {item["run_id"] for item in payload["runs"]} == {
        "g0-report-ok", "g0-report-timeout", "g0-report-invalid",
    }
    assert len(payload["groups"]) == 1
    group = payload["groups"][0]
    assert group["attempted"] == 3
    assert group["evaluation_valid"] == 1
    assert group["invalid"] == 2
    output = capsys.readouterr().out
    assert "attempted=3" in output
    assert "valid=1" in output
    assert "strict=1" in output
    assert "completed_within_budget=1" in output
    assert "invalid=2" in output
    assert "timeout=1" in output
    assert "agent:timeout=1" in output
    assert "verifier:evaluation_invalid=1" in output


def test_commands_refuse_to_write_inside_run_dirs(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _write_g0_run(runs, "g0-read-only")
    before = _tree_state([runs])

    inside = run / "fingerprints.json"
    assert experiments.main(["manifest", str(run), "--json", str(inside)]) == 2
    assert not inside.exists()
    runs_root_out = runs / "report.json"
    assert experiments.main(["report", str(run), "--json", str(runs_root_out)]) == 2
    assert not runs_root_out.exists()
    assert _tree_state([runs]) == before


def test_manifest_derives_business_anchor_and_start_snapshot(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _write_g0_run(runs, "g0-recovery", anchor=None, snapshot=None)
    trial = run / "harbor/job/trial-1"
    plan = trial / "agent/recovery" / ("a" * 64)
    plan.mkdir(parents=True)
    (trial / "result.json").write_text("{}", encoding="utf-8")
    (plan / "request.json").write_text(
        json.dumps({"anchor_date": "2026-03-04"}), encoding="utf-8"
    )
    (plan / "binding.json").write_text(
        json.dumps({"snapshot_sha256": "b" * 64}), encoding="utf-8"
    )
    out = tmp_path / "fingerprints.json"

    assert experiments.main(["manifest", str(run), "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    business = payload["runs"][0]["fingerprint"]["business"]
    assert business["anchor_date"] == "2026-03-04"
    assert "request.json" in business["anchor_source"]
    assert business["start_snapshot_sha256"] == "b" * 64
    assert "binding.json" in business["snapshot_source"]


def test_manifest_reads_tool_calls_from_trajectory(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    run = _write_g0_run(runs, "g0-tools", tool_calls=None)
    trial = run / "harbor/job/trial-1"
    agent_dir = trial / "agent"
    agent_dir.mkdir(parents=True)
    (trial / "result.json").write_text("{}", encoding="utf-8")
    (agent_dir / "pi.txt").write_text(
        "\n".join([
            json.dumps({"type": "tool_execution_start"}),
            json.dumps({"type": "message_start"}),
            json.dumps({"type": "tool_execution_start"}),
        ]) + "\n",
        encoding="utf-8",
    )
    out = tmp_path / "fingerprints.json"

    assert experiments.main(["manifest", str(run), "--json", str(out)]) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    product = payload["runs"][0]["product"]
    assert product["tool_calls"] == 2
    assert "pi.txt" in product["tool_calls_source"]


def test_continuous_budget_and_frozen_runtime_are_separate_comparison_factors(tmp_path):
    runs = tmp_path / "runs"
    runs.mkdir()
    old = _write_g0_run(runs, "legacy")
    new = _write_g0_run(runs, "continuous")
    path = new / "manifest.json"
    manifest = json.loads(path.read_text())
    manifest["budget"].update(mechanism="host-continuous-v1", cleanup_allowance_sec=30,
                              agent_outer_timeout_sec=630, agent_setup_outer_timeout_sec=1830)
    manifest["frozen_execution"] = {
        "execution_uses_snapshot": True, "index_sha256": "b" * 64,
        "host_dependencies_frozen": True, "host_runtime": {
            "tree_sha256": "c" * 64, "lock_sha256": "d" * 64,
            "requirements_sha256": "e" * 64, "root": "/private/must-not-enter-fingerprint",
        },
    }
    path.write_text(json.dumps(manifest))
    old_record = experiments._fingerprint_record(old)
    new_record = experiments._fingerprint_record(new)
    mixed, fields = experiments._mixed_config([old_record, new_record])
    assert mixed
    assert "budget.mechanism" in fields
    assert "budget.agent_outer_timeout_sec" in fields
    assert "execution.host_runtime_tree_sha256" in fields
    assert old_record["fingerprint"]["budget"]["mechanism"] is None
    assert old_record["fingerprint"]["execution"]["execution_uses_snapshot"] is None
    assert "must-not-enter-fingerprint" not in json.dumps(new_record["fingerprint"])
    assert old_record["fingerprint"]["budget"]["agent_timeout_sec"] == 600

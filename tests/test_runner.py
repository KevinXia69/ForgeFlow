import hashlib
import json
import sys
import tarfile
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from forgeflow.config import resolve_execution_guidance
from forgeflow.runner import (
    PI_IMPORT_PATH,
    VERIFIER_IMPORT_PATH,
    build_run_command,
    capture_implementation_snapshot,
    compare_implementation_snapshot,
    run_task,
    stage_pi_task,
    stage_task,
    summarize_run,
)

pytestmark = pytest.mark.usefixtures("synthetic_host_runtime")


def write_trial(root, *, score=100, rule_status="PASS", agent="oracle", state="completed", exception=None):
    root.mkdir(exist_ok=True)
    manifest = {
        "run_id": root.name, "task": "2280_medium_repair_plan_medium",
        "agent": agent, "state": state, "harbor_returncode": 0,
    }
    (root / "manifest.json").write_text(json.dumps(manifest))
    trial = root / "harbor/job/2280_trial"
    (trial / "verifier").mkdir(parents=True)
    reward = {
        "overall_score": score, "passed": score >= 99,
        "rules": {"by_dimension": {"constraint": [
            {"status": rule_status, "applicable": True},
            {"status": "NA", "applicable": False},
        ]}},
    }
    (trial / "verifier/reward.json").write_text(json.dumps(reward))
    (trial / "verifier/reward.txt").write_text(str(score))
    (trial / "result.json").write_text(json.dumps({
        "task_name": "agentic-labs/2280_medium_repair_plan_medium",
        "exception_info": exception, "verifier_result": {"rewards": {"reward": score}},
        "agent_result": {"n_input_tokens": 123, "cost_usd": None},
    }))
    return trial


def test_command_has_one_attempt_and_explicit_agent_budget(tmp_path):
    command = build_run_command(tmp_path, "A", "oracle", tmp_path / "runs/test", agent_timeout_sec=900)
    assert command[command.index("-n") + 1] == "1"
    assert command[command.index("-k") + 1] == "1"
    assert command[command.index("--max-retries") + 1] == "0"
    assert command[command.index("--agent-timeout-multiplier") + 1] == "0.25"
    assert command[command.index("--verifier") + 1] == VERIFIER_IMPORT_PATH
    assert command[command.index("--jobs-dir") + 1] == str(tmp_path / "runs/test/harbor")
    assert "--mounts" not in command


def test_pi_command_contains_model_but_no_credentials(tmp_path):
    command = build_run_command(tmp_path, "B", "pi", tmp_path / "run", model="nvidia/example")
    assert command[command.index("-a") + 1] == PI_IMPORT_PATH
    assert command[command.index("-m") + 1] == "nebius/nvidia/example"
    assert not any("API_KEY" in value for value in command)


def test_pi_command_passes_explicit_output_budget_as_adapter_kwarg(tmp_path):
    command = build_run_command(
        tmp_path, "A", "pi", tmp_path / "run",
        model="nvidia/example", max_output_tokens=32768,
    )
    assert command[-2:] == ["--ak", "max_output_tokens=32768"]
    assert command[command.index("--agent-timeout-multiplier") + 1] == str(3630 / 3600)
    assert "agent_budget_sec=3600" in command


def test_thinking_mode_is_opt_in_and_changes_only_adapter_kwarg(tmp_path):
    kwargs = {"model": "nvidia/example", "planner_profile": "cp-sat-native-v1"}
    baseline = build_run_command(tmp_path, "A", "pi", tmp_path / "run", **kwargs)
    explicit_default = build_run_command(
        tmp_path, "A", "pi", tmp_path / "run", **kwargs, thinking_mode="provider-default",
    )
    assert explicit_default == baseline
    changed = build_run_command(
        tmp_path, "A", "pi", tmp_path / "run", **kwargs, thinking_mode="off",
    )
    position = changed.index("thinking_mode=off")
    assert changed[position - 1] == "--ak"
    del changed[position - 1:position + 1]
    assert changed == baseline
    assert not any("max_output_tokens=" in part for part in changed)


@pytest.mark.parametrize(
    ("agent", "mode"),
    [("oracle", "off"), ("nop", "off"), ("pi", "high"), ("pi", None), ("pi", {})],
)
def test_invalid_thinking_mode_rejected_before_run_creation(tmp_path, agent, mode):
    with pytest.raises(ValueError, match="thinking_mode"):
        run_task(tmp_path, "A", agent, thinking_mode=mode)
    with pytest.raises(ValueError, match="thinking_mode"):
        build_run_command(tmp_path, "A", agent, tmp_path / "run", thinking_mode=mode)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("profile", ["odoo-read-v1", "odoo-schedule-v1"])
def test_guidance_is_pi_only_opt_in_and_independent_of_output_budget(tmp_path, profile):
    kwargs = {"model": "nvidia/example"}
    baseline = build_run_command(tmp_path, "A", "pi", tmp_path / "run", **kwargs)
    guided = build_run_command(
        tmp_path, "A", "pi", tmp_path / "run", **kwargs, execution_guidance=profile,
    )
    assert guided == baseline + ["--ak", f"execution_guidance={profile}"]
    assert not any("max_output_tokens" in item for item in guided)
    with pytest.raises(ValueError, match="only for the Pi agent"):
        run_task(tmp_path, "A", "oracle", execution_guidance=profile)
    with pytest.raises(ValueError, match="Unknown execution guidance"):
        run_task(tmp_path, "A", "pi", execution_guidance="unreviewed")
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("planner_profile", ["cp-sat-live-v1", "cp-sat-native-v1", "cp-sat-native-v2", "cp-sat-recovery-v1"])
def test_planner_is_pi_only_opt_in_and_independent_of_other_profiles(tmp_path, planner_profile):
    baseline = build_run_command(tmp_path, "A", "pi", tmp_path / "run", model="nvidia/example")
    planned = build_run_command(
        tmp_path, "A", "pi", tmp_path / "run", model="nvidia/example",
        planner_profile=planner_profile,
    )
    assert planned == baseline + ["--ak", f"planner_profile={planner_profile}"]
    assert not any("max_output_tokens" in item or "execution_guidance" in item for item in planned)
    for agent, profile, message in (
        ("oracle", planner_profile, "only for the Pi agent"),
        ("nop", planner_profile, "only for the Pi agent"),
        ("pi", "unreviewed", "Unknown planner profile"),
    ):
        with pytest.raises(ValueError, match=message):
            run_task(tmp_path, "A", agent, planner_profile=profile)
        with pytest.raises(ValueError, match=message):
            build_run_command(tmp_path, "A", agent, tmp_path / "run", planner_profile=profile)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize(
    ("agent", "value"),
    [("oracle", 32768), ("nop", 16384), ("pi", True), ("pi", 0), ("pi", -1), ("pi", 1.5)],
)
def test_invalid_output_budget_rejected_before_creating_run(tmp_path, agent, value):
    with pytest.raises(ValueError, match="max_output_tokens"):
        run_task(tmp_path, "A", agent, max_output_tokens=value)
    with pytest.raises(ValueError, match="max_output_tokens"):
        build_run_command(tmp_path, "A", agent, tmp_path / "run", max_output_tokens=value)
    assert not (tmp_path / "runs").exists()


def make_task_fixture(root):
    task = root / "vendor/erp-bench/tasks/2280_medium_repair_plan_medium"
    task.mkdir(parents=True)
    (task / "task.toml").write_text(
        'schema_version = "1.1"\n'
        '[agent]\ntimeout_sec = 3600.0\n'
        '[verifier]\ntimeout_sec = 300.0\n'
        '[environment]\nallow_internet = true\n'
    )
    for name, content in {
        "instruction.md": "Repair existing orders.",
        "environment/Dockerfile": (
            'FROM odoo:19\nRUN uv pip install --system --no-cache \\\n'
            '    "odoo-client-lib==2.0.0"\n'
        ),
        "environment/scenario_data.json": '{"fixture":true}',
        "solution/optimal_plan.json": '{"reference":true}',
        "solution/solver.py": "print('reference')\n",
        "tests/test.sh": "echo original-verifier\n",
        "tests/checks.py": "print('original checks')\n",
    }.items():
        path = task / name
        path.parent.mkdir(exist_ok=True)
        path.write_text(content)
    return task


def add_planning_workcenters(task):
    payload = {
        "untouched": {"quantity": 4.0, "other": [1, 2]},
        "workcenters": [
            {"code": "shared", "product_code": "FINAL", "time_per_unit_minutes": 117.55,
             "unit_cost": 31.0, "lead_days": 5.0, "note": "Original outage note.\n"},
            {"code": "shared", "product_code": "CHILD", "time_per_unit_minutes": 22,
             "unit_cost": 10.5, "lead_days": 2, "note": "Other product context"},
            {"code": "alternative", "product_code": "FINAL", "time_per_unit_minutes": 127.55,
             "unit_cost": 36.0, "lead_days": 5.0, "note": None},
        ],
    }
    (task / "environment/scenario_data.json").write_text(
        json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
    )
    return payload


def test_pi_staging_composes_build_fix_and_network_policy_and_preserves_original(tmp_path):
    original = make_task_fixture(tmp_path)
    before = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staged, record = stage_pi_task(tmp_path, "A", run_dir, allowed_host="api.tokenfactory.nebius.com")
    assert staged == run_dir / "task/2280_medium_repair_plan_medium"
    after = {str(p.relative_to(original)): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    assert after == before
    for relative, data in before.items():
        if relative not in ("task.toml", "environment/Dockerfile"):
            assert (staged / relative).read_bytes() == data
            assert record["original_file_sha256"][relative] == record["execution_file_sha256"][relative]
    source_config = tomllib.loads(before["task.toml"].decode())
    execution_config = tomllib.loads((staged / "task.toml").read_text())
    expected = source_config.copy()
    expected["agent"] = {
        **source_config["agent"], "network_mode": "allowlist",
        "allowed_hosts": ["api.tokenfactory.nebius.com"],
    }
    assert execution_config == expected
    assert record["original_files_modified"] is False
    assert record["data_profile"] == {
        "name": "original", "visibility_only": False, "is_original_benchmark_data": True,
    }
    assert record["derived_config_modified"] is True
    assert record["unchanged_artifacts_verified"] is True
    assert record["expected_modified_files"] == ["environment/Dockerfile", "task.toml"]
    dockerfile = (staged / "environment/Dockerfile").read_text()
    assert dockerfile == before["environment/Dockerfile"].decode().replace(
        "RUN uv pip install --system --no-cache",
        "RUN uv pip install --system --break-system-packages --no-cache",
    )
    assert "ENV " not in dockerfile
    assert '"odoo-client-lib==2.0.0"' in dockerfile
    assert "PEP 668" in record["execution_patches"][0]["reason"]
    command = build_run_command(tmp_path, "A", "pi", run_dir, model="nvidia/example", task_path=staged)
    assert command[command.index("-p") + 1] == str(staged)
    assert not any("mount" in part for part in command)


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_control_staging_changes_only_build_install_flag(tmp_path, agent):
    original = make_task_fixture(tmp_path)
    before = {p.relative_to(original).as_posix(): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    staged, record = stage_task(tmp_path, "A", tmp_path, agent=agent)
    assert record["expected_modified_files"] == ["environment/Dockerfile"]
    assert record["derived_config_modified"] is False
    assert record["execution_config_overrides"] == {}
    assert len(record["execution_patches"]) == 1
    for relative, data in before.items():
        assert (original / relative).read_bytes() == data
        if relative != "environment/Dockerfile":
            assert (staged / relative).read_bytes() == data
            assert record["original_file_sha256"][relative] == record["execution_file_sha256"][relative]
    patch = record["execution_patches"][0]
    assert (staged / patch["path"]).read_text() == before[patch["path"]].decode().replace(
        patch["before"], patch["after"], 1,
    )


@pytest.mark.parametrize("agent", ["oracle", "nop", "pi"])
def test_planning_profile_exposes_shared_center_fields_only_in_notes(tmp_path, agent):
    original = make_task_fixture(tmp_path)
    payload = add_planning_workcenters(original)
    before = {p.relative_to(original).as_posix(): p.read_bytes() for p in original.rglob("*") if p.is_file()}
    staged, record = stage_task(
        tmp_path, "A", tmp_path, agent=agent, data_profile="planning-metadata-v1",
        allowed_host="api.nebius.com" if agent == "pi" else None,
    )
    projected = json.loads((staged / "environment/scenario_data.json").read_text())
    for index, row in enumerate(projected["workcenters"]):
        original_row = payload["workcenters"][index]
        note = row["note"]
        if original_row["note"]:
            assert note.startswith(original_row["note"])
        assert "benchmark horizon-wide capacity" in note
        assert "Do not substitute calendar-recomputed workcenter_load" in note
        disclosed = [json.loads(line) for line in note.splitlines() if line.startswith("{")]
        expected = [
            {k: source[k] for k in ("product_code", "time_per_unit_minutes", "unit_cost", "lead_days")}
            for source in payload["workcenters"] if source["code"] == row["code"]
        ]
        assert disclosed == expected
        row["note"] = original_row["note"]
    assert projected == payload
    profile = record["data_profile"]
    assert profile["visibility_only"] is True
    assert profile["is_original_benchmark_data"] is False
    assert profile["source_sha256"] == hashlib.sha256(before["environment/scenario_data.json"]).hexdigest()
    assert profile["execution_sha256"] == record["execution_file_sha256"]["environment/scenario_data.json"]
    diff = (tmp_path / profile["diff_path"]).read_bytes()
    assert profile["diff_sha256"] == hashlib.sha256(diff).hexdigest()
    assert b"planning-metadata-v1/environment/scenario_data.json" in diff
    for group in profile["field_sources"]:
        for source in group["rows"]:
            for field, pointer in source["field_pointers"].items():
                _, _, index, pointer_field = pointer.split("/")
                assert pointer_field == field
                assert payload["workcenters"][int(index)][field] == (
                    group["workcenter_code"] if field == "code" else source["values"][field]
                )
    changed = {"environment/Dockerfile", "environment/scenario_data.json"}
    if agent == "pi":
        changed.add("task.toml")
    assert set(record["expected_modified_files"]) == changed
    for relative, content in before.items():
        assert (original / relative).read_bytes() == content
        if relative not in changed:
            assert (staged / relative).read_bytes() == content


@pytest.mark.parametrize("bad_value", [None, -1, True, float("nan"), "guess"])
def test_planning_profile_rejects_missing_or_invalid_source_values_before_copy(tmp_path, bad_value):
    original = make_task_fixture(tmp_path)
    payload = add_planning_workcenters(original)
    payload["workcenters"][1]["lead_days"] = bad_value
    (original / "environment/scenario_data.json").write_text(json.dumps(payload))
    with pytest.raises(ValueError, match="existing lead_days"):
        stage_task(tmp_path, "A", tmp_path, agent="oracle", data_profile="planning-metadata-v1")
    assert not (tmp_path / "task").exists()


def test_unknown_data_profile_rejected_before_creating_run(tmp_path):
    with pytest.raises(ValueError, match="Unknown data profile"):
        run_task(tmp_path, "A", "oracle", data_profile="unreviewed")
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("command", [
    "RUN uv pip install --system --no-cache",
    "RUN uv pip install --system --no-cache \\\nRUN uv pip install --system --no-cache \\\n",
])
def test_staging_rejects_changed_or_ambiguous_upstream_install_command(tmp_path, command):
    original = make_task_fixture(tmp_path)
    (original / "environment/Dockerfile").write_text(command)
    with pytest.raises(ValueError, match="uv install command"):
        stage_task(tmp_path, "A", tmp_path, agent="oracle")
    assert not (tmp_path / "task").exists()


def test_harbor_resolves_allowlist_only_for_agent_execution(tmp_path):
    from harbor.models.task.config import TaskConfig, VerifierEnvironmentMode
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig
    from harbor.trial.network_policy import resolve_trial_network_plan

    make_task_fixture(tmp_path)
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    staged, _ = stage_pi_task(tmp_path, "A", run_dir, allowed_host="api.tokenfactory.nebius.com")
    with pytest.warns(DeprecationWarning, match="allow_internet"):
        task_config = TaskConfig.model_validate_toml((staged / "task.toml").read_text())
    plan = resolve_trial_network_plan(
        task_config, AgentConfig(name=PI_IMPORT_PATH), EnvironmentConfig(type="docker"),
        None, verifier_mode=VerifierEnvironmentMode.SHARED,
    )
    assert plan.agent_env_baseline.network_mode.value == "public"
    assert plan.agent_phase.network_mode.value == "allowlist"
    assert plan.agent_phase.allowed_hosts == ["api.tokenfactory.nebius.com"]
    assert plan.verifier_phase.network_mode.value == "public"


@pytest.mark.parametrize("hostname", ["*.nebius.com", "https://api.nebius.com", "api.nebius.com\nx", "0.0.0.0/0", ""])
def test_staging_rejects_broad_or_invalid_host_entries(tmp_path, hostname):
    make_task_fixture(tmp_path)
    with pytest.raises(ValueError):
        stage_pi_task(tmp_path, "A", tmp_path, allowed_host=hostname)


def test_staging_never_overwrites_existing_task_copy(tmp_path):
    make_task_fixture(tmp_path)
    staged, _ = stage_pi_task(tmp_path, "A", tmp_path, allowed_host="api.nebius.com")
    before = (staged / "task.toml").read_bytes()
    with pytest.raises(FileExistsError):
        stage_pi_task(tmp_path, "A", tmp_path, allowed_host="api.other.nebius.com")
    assert (staged / "task.toml").read_bytes() == before


def test_staging_detects_modified_copied_business_artifacts(tmp_path, monkeypatch):
    import shutil

    from forgeflow import runner

    original = make_task_fixture(tmp_path)
    actual_copytree = shutil.copytree

    def corrupt_copy(source, destination, *args, **kwargs):
        result = actual_copytree(source, destination, *args, **kwargs)
        if (Path(destination) / "instruction.md").exists():
            (Path(destination) / "instruction.md").write_text("changed")
        return result

    monkeypatch.setattr(runner.shutil, "copytree", corrupt_copy)
    with pytest.raises(RuntimeError, match="business artifacts"):
        stage_pi_task(tmp_path, "A", tmp_path, allowed_host="api.nebius.com")
    assert (original / "instruction.md").read_text() == "Repair existing orders."


@pytest.mark.parametrize("task,agent", [("../answer", "oracle"), ("A", "claude-code")])
def test_rejects_unselected_tasks_and_agents(tmp_path, task, agent):
    with pytest.raises(ValueError):
        build_run_command(tmp_path, task, agent, tmp_path / "run")


@pytest.mark.parametrize("timeout", [0, -1, True, 0.5])
def test_budget_must_be_positive_integer(tmp_path, timeout):
    with pytest.raises(ValueError):
        build_run_command(tmp_path, "A", "oracle", tmp_path / "run", agent_timeout_sec=timeout)


@pytest.mark.parametrize("score,passed", [(0, False), (1, False), (98.99, False), (99, True), (100, True)])
def test_preserves_zero_to_hundred_original_score(tmp_path, score, passed):
    write_trial(tmp_path, score=score)
    summary = summarize_run(tmp_path)
    assert summary["score"] == score
    assert summary["original_passed"] is passed
    assert summary["strict_success"] is passed
    assert summary["metrics"]["cost_usd"] is None


def test_strict_success_requires_every_applicable_rule(tmp_path):
    write_trial(tmp_path, score=99, rule_status="FAIL")
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is True
    assert summary["strict_success"] is False
    assert summary["failed_rule_count"] == 1


def test_nop_failure_is_an_expected_environment_control(tmp_path):
    write_trial(tmp_path, score=0, rule_status="FAIL", agent="nop")
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is False
    assert summary["strict_success"] is False
    assert summary["environment_chain_ok"] is True
    assert summary["expected_control_outcome"] is True


def test_missing_results_remain_unknown(tmp_path):
    summary = summarize_run(tmp_path)
    for key in ("score", "original_passed", "strict_success", "environment_chain_ok", "expected_control_outcome"):
        assert summary[key] is None


def test_original_reward_does_not_hide_harbor_exception(tmp_path):
    write_trial(tmp_path, exception={"exception_type": "VerifierOutputParseError", "exception_message": "bad format"})
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is True
    assert summary["raw_task_strict_success"] is True
    assert summary["strict_success"] is None
    assert summary["expected_control_outcome"] is None
    assert summary["environment_chain_ok"] is False
    assert summary["trial_exception"]["exception_type"] == "VerifierOutputParseError"


@pytest.mark.parametrize("state", ["preflight", "running", "failed", "interrupted", "unknown"])
def test_noncompleted_run_cannot_claim_a_valid_control(tmp_path, state):
    write_trial(tmp_path, score=0, rule_status="FAIL", agent="nop", state=state)
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is False
    assert summary["raw_task_strict_success"] is False
    assert summary["strict_success"] is None
    assert summary["expected_control_outcome"] is None
    assert summary["evaluation_valid"] is not True


def test_inconsistent_passed_flag_cannot_claim_valid_results(tmp_path):
    trial = write_trial(tmp_path, score=0, agent="nop")
    path = trial / "verifier/reward.json"
    reward = json.loads(path.read_text())
    reward["passed"] = True
    path.write_text(json.dumps(reward))
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is True
    assert summary["strict_success"] is None
    assert summary["expected_control_outcome"] is None
    assert summary["evaluation_valid"] is False


@pytest.mark.parametrize("verifier_result", [
    None,
    {},
    {"rewards": {}},
    {"rewards": {"reward": 0}},
    {"rewards": {"reward": float("nan")}},
    {"rewards": {"reward": True}},
    {"rewards": {"reward": "100"}},
    {"rewards": {"reward": 101}},
])
def test_harbor_reward_must_exist_and_match_original(tmp_path, verifier_result):
    trial = write_trial(tmp_path)
    path = trial / "result.json"
    result = json.loads(path.read_text())
    result["verifier_result"] = verifier_result
    path.write_text(json.dumps(result))
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is True
    assert summary["raw_task_strict_success"] is True
    assert summary["strict_success"] is None
    assert summary["environment_chain_ok"] is False
    assert summary["expected_control_outcome"] is None


def test_missing_rule_details_do_not_imply_strict_success(tmp_path):
    trial = write_trial(tmp_path)
    (trial / "verifier/reward.json").write_text('{"overall_score":100,"passed":true}')
    summary = summarize_run(tmp_path)
    assert summary["strict_success"] is None
    assert summary["expected_control_outcome"] is None


def test_invalid_score_does_not_report_strict_success(tmp_path):
    trial = write_trial(tmp_path)
    reward_path = trial / "verifier/reward.json"
    reward = json.loads(reward_path.read_text())
    reward["overall_score"] = float("nan")
    reward_path.write_text(json.dumps(reward))
    summary = summarize_run(tmp_path)
    assert summary["score"] is None
    assert summary["strict_success"] is None
    assert summary["environment_chain_ok"] is False


def test_invalid_applicability_cannot_hide_a_failed_rule(tmp_path):
    trial = write_trial(tmp_path, rule_status="FAIL")
    reward_path = trial / "verifier/reward.json"
    reward = json.loads(reward_path.read_text())
    reward["rules"]["by_dimension"]["constraint"][0]["applicable"] = False
    reward_path.write_text(json.dumps(reward))
    summary = summarize_run(tmp_path)
    assert summary["strict_success"] is None
    assert summary["environment_chain_ok"] is False


def make_fake_workspace(tmp_path):
    make_task_fixture(tmp_path)
    (tmp_path / ".venv/bin").mkdir(parents=True)
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts/fetch_upstream.py").write_text("print('verified pinned source')\n")
    (tmp_path / "upstream.lock.json").write_text('{"sources":{"test":{"commit":"fixture"}}}')
    (tmp_path / "src/forgeflow/ui_assets").mkdir(parents=True)
    (tmp_path / "src/forgeflow/__init__.py").write_text("# fixture implementation\n")
    (tmp_path / "src/forgeflow/runner.py").write_text("# fixture runner\n")
    (tmp_path / "src/forgeflow/verifier.py").write_text("# fixture verifier\n")
    (tmp_path / "src/forgeflow/agents").mkdir()
    (tmp_path / "src/forgeflow/agents/__init__.py").write_text("")
    (tmp_path / "src/forgeflow/agents/pi_nebius.py").write_text("# fixture adapter\n")
    (tmp_path / "src/forgeflow/ui_assets/app.js").write_text("// initial UI\n")
    (tmp_path / "pyproject.toml").write_text('[project]\nname = "fixture"\n')
    (tmp_path / "uv.lock").write_text("version = 1\n")
    model_data = tmp_path / "build-inputs/pi-0.85.1-model-data"
    (model_data / "data").mkdir(parents=True)
    (model_data / "provenance.json").write_text('{"source":"fixture"}')
    (model_data / "data/provider.json").write_text('{"models":[]}')
    planner = tmp_path / "build-inputs/planner"
    planner.mkdir()
    (planner / "requirements.in").write_text("ortools==9.14.6206\n")
    (planner / "requirements.txt").write_text("# fixture dependency hashes\n")
    # Synthetic pinned generic dependencies. No real vendor files are accessed.
    from forgeflow.planning.bundle import UPSTREAM_FILES

    sources = {}
    for source, names in {
        "erp-bench": {"agents/pi.py", *UPSTREAM_FILES},
        "pi-mono": {"package-lock.json", "packages/coding-agent/package.json"},
    }.items():
        entries = {}
        for name in sorted(names):
            path = tmp_path / "vendor" / source / name
            path.parent.mkdir(parents=True, exist_ok=True)
            content = b"{}\n" if name.endswith(".json") else b"# synthetic dependency\n"
            path.write_bytes(content)
            entries[name] = {"sha256": hashlib.sha256(content).hexdigest(),
                             "size": len(content), "executable": False}
        tree = hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        sources[source] = {"commit": "fixture", "tree_sha256": tree}
        (tmp_path / "vendor" / f"{source}.source.json").write_text(
            json.dumps({"commit": "fixture", "files": entries})
        )
    (tmp_path / "upstream.lock.json").write_text(json.dumps({"sources": sources}))
    (tmp_path / ".venv/bin/python").symlink_to(Path(sys.executable).resolve())
    executable = tmp_path / ".venv/bin/harbor"
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        "if '--print-config' in sys.argv:\n"
        "    print(json.dumps({'fixture': True})); raise SystemExit(0)\n"
        "key=os.environ.get('NEBIUS_API_KEY', '')\n"
        "print('sample output '+key, flush=True)\n"
        "root=Path(sys.argv[sys.argv.index('--jobs-dir')+1])/'job'/'2280_trial'\n"
        "(root/'verifier').mkdir(parents=True)\n"
        "reward={'overall_score':100, 'passed':True, 'rules':{'by_dimension':{'constraint':[{'status':'PASS','applicable':True}]}}}\n"
        "(root/'verifier/reward.json').write_text(json.dumps(reward))\n"
        "(root/'verifier/reward.txt').write_text('100')\n"
        "(root/'result.json').write_text(json.dumps({'task_name':'agentic-labs/2280_medium_repair_plan_medium','verifier_result':{'rewards':{'reward':100}},'exception_info':None}))\n"
        "(root/'trial.log').write_text('diagnostic '+key)\n"
        "(root/'environment.json').write_text(json.dumps({'key_present':bool(key),'other_provider_present':'OPENAI_API_KEY' in os.environ,'no_bytecode':os.environ.get('PYTHONDONTWRITEBYTECODE')}))\n"
    )
    executable.chmod(0o755)
    return executable


def test_snapshot_preserves_exact_files_and_excludes_credentials_caches_and_vendor(tmp_path):
    make_fake_workspace(tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=private-value\n")
    (tmp_path / "src/forgeflow/.env.local").write_text("private-value")
    (tmp_path / "src/forgeflow/__pycache__").mkdir()
    (tmp_path / "src/forgeflow/__pycache__/cached.pyc").write_bytes(b"cache")
    run_dir = tmp_path / "runs/snapshot"
    run_dir.mkdir(parents=True)
    snapshot = capture_implementation_snapshot(tmp_path, run_dir, secrets=("private-value",))
    index = json.loads((run_dir / snapshot["index_path"]).read_text())
    with tarfile.open(run_dir / snapshot["path"]) as archive:
        assert set(archive.getnames()) == set(index["files"])
        assert "src/forgeflow/__init__.py" in archive.getnames()
        assert "build-inputs/pi-0.85.1-model-data/data/provider.json" in archive.getnames()
        assert "build-inputs/planner/requirements.in" in archive.getnames()
        assert "build-inputs/planner/requirements.txt" in archive.getnames()
        for name in archive.getnames():
            content = archive.extractfile(name).read()
            assert content == (tmp_path / name).read_bytes()
            assert hashlib.sha256(content).hexdigest() == index["files"][name]["sha256"]
            assert b"private-value" not in content
            assert not any(part in name for part in (".env", "__pycache__", "vendor/", "runs/"))
    assert snapshot["execution_uses_snapshot"] is False
    assert snapshot["workspace_matches_at_capture"] is True
    assert compare_implementation_snapshot(tmp_path, run_dir, snapshot)["changed"] is False


def test_snapshot_rejects_symlink_and_configured_secret_in_source(tmp_path):
    make_fake_workspace(tmp_path)
    run_dir = tmp_path / "runs/snapshot"
    run_dir.mkdir(parents=True)
    alias = tmp_path / "scripts/outside.py"
    alias.symlink_to(tmp_path / ".env")
    with pytest.raises(ValueError, match="regular files"):
        capture_implementation_snapshot(tmp_path, run_dir)
    alias.unlink()
    (tmp_path / "src/forgeflow/__init__.py").write_text("private-source-key")
    with pytest.raises(ValueError, match="credential"):
        capture_implementation_snapshot(tmp_path, run_dir, secrets=("private-source-key",))
    assert not (run_dir / "implementation").exists()


def test_ui_change_after_capture_is_recorded_without_overriding_business_result(tmp_path):
    executable = make_fake_workspace(tmp_path)
    with executable.open("a") as handle:
        target = str(tmp_path / "src/forgeflow/ui_assets/app.js")
        handle.write(f"Path({target!r}).write_text('// edited during run')\n")
    run_dir = run_task(tmp_path, "A", "oracle", run_id="ui-changed")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    snapshot = manifest["implementation_snapshot"]
    after = snapshot["workspace_after"]
    assert manifest["state"] == "completed"
    assert after["changed_files"] == ["src/forgeflow/ui_assets/app.js"]
    assert after["ui_changed_files"] == ["src/forgeflow/ui_assets/app.js"]
    assert after["other_changed_files"] == []
    assert after["used_to_override_business_result"] is False
    assert after["snapshot_archive_unchanged"] is True
    assert after["snapshot_index_unchanged"] is True
    frozen = manifest["frozen_execution"]
    assert frozen["execution_uses_snapshot"] is True
    assert frozen["integrity_after"]["ok"] is True
    assert (run_dir / frozen["root"] / "src/forgeflow/ui_assets/app.js").read_text() == "// initial UI\n"
    assert summarize_run(run_dir)["strict_success"] is True
    with tarfile.open(run_dir / snapshot["path"]) as archive:
        assert archive.extractfile("src/forgeflow/ui_assets/app.js").read() == b"// initial UI\n"


@pytest.mark.parametrize(
    ("output_budget", "execution_guidance", "thinking_mode"),
    [
        (None, "none", "provider-default"), (32768, "none", "provider-default"),
        (None, "odoo-read-v1", "provider-default"), (None, "none", "off"),
        (None, "odoo-schedule-v1", "off"),
        (32768, "none", "off"),
    ],
)
def test_run_injects_only_nebius_and_redacts_all_recorded_output(
    tmp_path, monkeypatch, output_budget, execution_guidance, thinking_mode,
):
    make_fake_workspace(tmp_path)
    secret = "nebius-only-test-credential-123456"
    (tmp_path / ".env").write_text(f"NEBIUS_API_KEY={secret}\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "unrelated-secret")
    run_dir = run_task(
        tmp_path, "A", "pi", run_id="fixture",
        max_output_tokens=output_budget, execution_guidance=execution_guidance,
        thinking_mode=thinking_mode,
    )
    summary = json.loads((run_dir / "summary.json").read_text())
    assert summary["state"] == "completed"
    assert summary["strict_success"] is True
    assert summary["environment_chain_ok"] is True
    for path in run_dir.rglob("*"):
        if path.is_file():
            assert secret.encode() not in path.read_bytes()
            assert b"unrelated-secret" not in path.read_bytes()
    environment = json.loads((run_dir / "harbor/job/2280_trial/environment.json").read_text())
    assert environment == {"key_present": True, "other_provider_present": False, "no_bytecode": "1"}
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["thinking_mode"] == thinking_mode
    assert "harbor/job/2280_trial/trial.log" in manifest["redacted_artifacts"]
    assert secret not in " ".join(manifest["command"])
    assert manifest["execution_config_overrides"] == {
        "agent.network_mode": "allowlist",
        "agent.allowed_hosts": ["api.tokenfactory.nebius.com"],
    }
    assert manifest["task_derivation"]["derived_task_integrity_after"] is True
    assert manifest["task_derivation"]["original_files_modified"] is False
    assert manifest["data_profile"]["name"] == "original"
    assert manifest["budget"]["agent_setup_timeout_sec"] == 1800
    assert manifest["budget"]["agent_timeout_sec"] == 3600
    assert manifest["budget"]["max_output_tokens"] == output_budget
    assert manifest["budget"]["max_output_tokens_source"] == (
        "provider_default" if output_budget is None else "explicit_run_argument"
    )
    assert manifest["budget"]["output_limit_request_fields"] == (
        "omitted" if output_budget is None else "included"
    )
    assert manifest["budget"]["output_policy_version"] == (
        "all-requests-v2" if output_budget is None else "explicit"
    )
    assert manifest["budget"]["context_window_tokens"] == 131072
    expected_sampling = {"temperature": 1.0, "top_p": 0.95}
    if thinking_mode == "off":
        expected_sampling["chat_template_kwargs"] = {"enable_thinking": False}
    assert manifest["budget"]["sampling_params"] == expected_sampling
    assert ("thinking_mode=off" in manifest["command"]) == (thinking_mode == "off")
    assert "thinking_mode=provider-default" not in manifest["command"]
    if output_budget is None:
        assert not any("max_output_tokens=" in value for value in manifest["command"])
    else:
        assert f"max_output_tokens={output_budget}" in manifest["command"]
    assert manifest["execution_guidance"] == resolve_execution_guidance(execution_guidance)
    assert (f"execution_guidance={execution_guidance}" in manifest["command"]) == (execution_guidance != "none")
    assert "--override-setup-timeout-sec" not in manifest["command"]
    assert manifest["runtime"]["python_version"]
    assert manifest["runtime"]["host_architecture"]
    assert manifest["command"][manifest["command"].index("-p") + 1] == str(
        run_dir / "task/2280_medium_repair_plan_medium"
    )


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_control_manifest_has_no_applicable_thinking_mode(tmp_path, agent):
    make_fake_workspace(tmp_path)
    run_dir = run_task(tmp_path, "A", agent, run_id="control")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["thinking_mode"] is None
    assert "sampling_params" not in manifest["budget"]
    assert not any("thinking_mode=" in part for part in manifest["command"])


def test_heavy_operation_contention_preserves_failure_without_launch(tmp_path):
    from forgeflow.runtime_lifecycle import exclusive_lease

    make_fake_workspace(tmp_path)
    with exclusive_lease(tmp_path / ".runtime/lifecycle/heavy-operation.lock"):
        run_dir = run_task(tmp_path, "A", "oracle", run_id="busy")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "failed"
    assert manifest["error"]["message"] == "operation_in_progress"
    assert not (run_dir / "harbor").exists()
    next_run = run_task(tmp_path, "A", "oracle", run_id="after-release")
    assert json.loads((next_run / "manifest.json").read_text())["state"] == "completed"


@pytest.mark.parametrize("planner_profile", ["none", "cp-sat-live-v1", "cp-sat-native-v1", "cp-sat-native-v2", "cp-sat-recovery-v1"])
def test_planning_profile_and_execution_guidance_are_independent_and_recorded(
    tmp_path, monkeypatch, planner_profile,
):
    make_fake_workspace(tmp_path)
    add_planning_workcenters(tmp_path / "vendor/erp-bench/tasks/2280_medium_repair_plan_medium")
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=fixture-key\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    run_dir = run_task(
        tmp_path, "A", "pi", run_id="visible-and-guided",
        data_profile="planning-metadata-v1", execution_guidance="odoo-read-v1",
        planner_profile=planner_profile,
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "completed"
    assert manifest["mode"] == "benchmark_visibility_experiment"
    assert manifest["data_profile"] == manifest["task_derivation"]["data_profile"]
    assert manifest["data_profile"]["name"] == "planning-metadata-v1"
    assert manifest["execution_guidance"] == resolve_execution_guidance("odoo-read-v1")
    assert "execution_guidance=odoo-read-v1" in manifest["command"]
    assert not any("data_profile=" in part for part in manifest["command"])
    assert manifest["task_derivation"]["derived_task_integrity_after"] is True
    planner_record = {
        "profile": planner_profile,
        "enabled": planner_profile != "none",
        "read_only": True,
        "experimental_factor": "solver_tool_availability",
    }
    if planner_profile in {"cp-sat-native-v1", "cp-sat-native-v2"}:
        planner_record.update(interface="pi_native_tool", experimental_factor="solver_tool_interface")
    if planner_profile == "cp-sat-native-v2":
        planner_record.update(error_feedback="structured-v1", experimental_factor="planner_error_feedback")
    if planner_profile == "cp-sat-recovery-v1":
        planner_record.update(read_only=False, interface="pi_native_tool",
                              experimental_factor="validated_change_set_execution",
                              execution="planned_documents_with_journal_and_readback")
    assert manifest["planner"] == planner_record
    assert (f"planner_profile={planner_profile}" in manifest["command"]) == (planner_profile != "none")
    summary = summarize_run(run_dir)
    assert summary["data_profile"] == "planning-metadata-v1"
    assert summary["mode"] == "benchmark_visibility_experiment"
    assert summary["planner_profile"] == planner_profile


def test_failed_preflight_is_saved_without_fabricating_a_score(tmp_path):
    make_fake_workspace(tmp_path)
    (tmp_path / "scripts/fetch_upstream.py").write_text("raise SystemExit(1)\n")
    run_dir = run_task(tmp_path, "A", "oracle", run_id="failed-source")
    summary = summarize_run(run_dir)
    assert summary["state"] == "failed"
    assert summary["score"] is None
    assert summary["strict_success"] is None
    assert summary["environment_chain_ok"] is False
    assert summary["runner_error"]["type"] == "RuntimeError"
    assert not (run_dir / "harbor").exists()


@pytest.mark.parametrize("profile", ["none", "reviewed-request-v1"])
def test_contract_guidance_reaches_child_only_when_explicit(tmp_path, monkeypatch, profile):
    from forgeflow.contract import build_contract
    from forgeflow.contract_guidance import resolve_contract_guidance

    executable = make_fake_workspace(tmp_path)
    executable.write_text(executable.read_text() +
        "(root/'guidance.json').write_text(json.dumps({'profile':os.environ.get('FORGEFLOW_CONTRACT_GUIDANCE'),"
        "'contract':json.loads(os.environ['FORGEFLOW_CONTRACT_JSON'])}))\n")
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=fixture-key\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("FORGEFLOW_CONTRACT_GUIDANCE", "ambient-must-not-leak")
    contract = build_contract(
        contract_id="fixture-reviewed-v1", task_id="2280_medium_repair_plan_medium",
        task_type="supplier_cancellation", product_code="P-TEST", anchor_date="2026-10-22",
        margin=0.25, margin_basis="portfolio_new_spend_margin_at_list_price",
        budgets={"1": 100}, sales_order_ids=[1],
        disruption={"kind": "supplier_cancellation", "vendor_ref": "V-1", "vendor_name": "Fixture Vendor"},
        original_commitment_policy="keep_unaffected_work", sources={"instruction": "fixture"},
    ).payload()
    contract_path = tmp_path / "reviewed.json"
    contract_path.write_text(json.dumps(contract))
    run_dir = run_task(
        tmp_path, "A", "pi", run_id="contract-guidance", contract_path=contract_path,
        contract_guidance=profile, planner_profile="cp-sat-recovery-v1",
    )
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "completed"
    assert manifest["contract_guidance"] == resolve_contract_guidance(profile, contract)
    child = json.loads((run_dir / "harbor/job/2280_trial/guidance.json").read_text())
    assert child["profile"] == (None if profile == "none" else profile)
    assert child["contract"] == contract


def test_setup_budget_metadata_requires_an_explicit_adapter_declaration(tmp_path, monkeypatch):
    from forgeflow.agents.pi_nebius import PiNebius

    make_fake_workspace(tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=test-nebius\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delattr(PiNebius, "DEFAULT_SETUP_TIMEOUT_SEC")
    run_dir = run_task(tmp_path, "A", "pi", run_id="inherited-setup-default")
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "completed"
    assert "agent_setup_timeout_sec" not in manifest["budget"]
    assert manifest["budget"]["agent_timeout_sec"] == 3600


def test_failed_harbor_process_is_preserved(tmp_path):
    executable = make_fake_workspace(tmp_path)
    executable.write_text(
        f"#!{Path(sys.executable).resolve()}\nimport sys\n"
        "if '--print-config' in sys.argv:\n"
        "    print('{}'); raise SystemExit(0)\n"
        "print('Docker unavailable'); raise SystemExit(3)\n"
    )
    run_dir = run_task(tmp_path, "A", "nop", run_id="docker-failure")
    summary = summarize_run(run_dir)
    assert summary["state"] == "failed"
    assert summary["harbor_returncode"] == 3
    assert summary["original_passed"] is None
    assert summary["expected_control_outcome"] is None
    assert "Docker unavailable" in (run_dir / "harbor-output.log").read_text()


def test_existing_run_is_never_overwritten(tmp_path):
    make_fake_workspace(tmp_path)
    run_dir = run_task(tmp_path, "A", "oracle", run_id="same")
    before = (run_dir / "manifest.json").read_bytes()
    with pytest.raises(FileExistsError):
        run_task(tmp_path, "A", "oracle", run_id="same")
    assert (run_dir / "manifest.json").read_bytes() == before


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_environment_controls_use_only_the_compatibility_task_copy(tmp_path, agent):
    make_fake_workspace(tmp_path)
    run_dir = run_task(tmp_path, "A", agent, run_id=agent)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["task_derivation"]["expected_modified_files"] == ["environment/Dockerfile"]
    assert manifest["task_derivation"]["derived_task_integrity_after"] is True
    assert manifest["execution_config_overrides"] == {}
    assert "agent_setup_timeout_sec" not in manifest["budget"]
    assert manifest["command"][manifest["command"].index("-p") + 1] == str(
        run_dir / "task/2280_medium_repair_plan_medium"
    )


def test_child_environment_uses_local_docker_settings_without_other_file_secrets(tmp_path, monkeypatch):
    from forgeflow.runner import _child_environment

    for name in ("DOCKER_HOST", "DOCKER_CONTEXT", "DOCKER_CONFIG"):
        monkeypatch.delenv(name, raising=False)
    (tmp_path / ".env").write_text(
        "DOCKER_HOST=unix:///tmp/forgeflow-docker.sock\n"
        "DOCKER_CONFIG=/tmp/forgeflow-docker-config\n"
        "OPENAI_API_KEY=unrelated-key\nNEBIUS_API_KEY=only-for-model-phase\n"
    )
    environment = _child_environment(tmp_path)
    assert environment["DOCKER_HOST"] == "unix:///tmp/forgeflow-docker.sock"
    assert environment["DOCKER_CONFIG"] == "/tmp/forgeflow-docker-config"
    assert "OPENAI_API_KEY" not in environment
    assert "NEBIUS_API_KEY" not in environment


def test_mismatched_trial_identity_cannot_claim_success(tmp_path):
    trial = write_trial(tmp_path)
    path = trial / "result.json"
    result = json.loads(path.read_text())
    result["task_name"] = "agentic-labs/2299_hard_repair_plan_hard"
    path.write_text(json.dumps(result))
    summary = summarize_run(tmp_path)
    assert summary["original_passed"] is True
    assert summary["environment_chain_ok"] is False
    assert summary["strict_success"] is None
    assert summary["expected_control_outcome"] is None


def test_stop_group_tolerates_exit_before_signal(tmp_path, monkeypatch):
    from forgeflow import runner

    process = SimpleNamespace(pid=123456789, returncode=0, wait=lambda timeout: 0)

    def already_exited(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(runner.os, "killpg", already_exited)
    assert runner._stop_process_group(process) == []


def test_stop_group_escalates_a_process_that_ignores_term(monkeypatch):
    from forgeflow import runner

    signals = []
    waits = []

    def wait(timeout):
        waits.append(timeout)
        if len(waits) == 1:
            raise runner.subprocess.TimeoutExpired("harbor", timeout)
        return -9

    process = SimpleNamespace(pid=123456789, returncode=None, wait=wait)
    monkeypatch.setattr(runner.os, "killpg", lambda pid, sig: signals.append(sig))
    assert runner._stop_process_group(process) == []
    assert signals == [runner.signal.SIGTERM, runner.signal.SIGKILL]
    assert waits == [30, 10]


def test_interrupt_preserves_run_even_when_process_group_already_exited(tmp_path, monkeypatch):
    from forgeflow import runner

    make_fake_workspace(tmp_path)

    class InterruptedOutput:
        def __iter__(self):
            raise KeyboardInterrupt()

        def close(self):
            pass

    process = SimpleNamespace(
        pid=123456789, returncode=0, stdout=InterruptedOutput(), wait=lambda timeout: 0,
    )
    monkeypatch.setattr(
        runner.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="{}", stderr=""),
    )
    monkeypatch.setattr(runner.subprocess, "Popen", lambda *args, **kwargs: process)

    def already_exited(pid, sig):
        raise ProcessLookupError()

    monkeypatch.setattr(runner.os, "killpg", already_exited)
    run_dir = run_task(tmp_path, "A", "oracle", run_id="interrupt-race")
    summary = summarize_run(run_dir)
    assert summary["state"] == "interrupted"
    assert summary["runner_error"]["type"] == "KeyboardInterrupt"
    assert summary["strict_success"] is None
    assert summary["expected_control_outcome"] is None
    assert summary["harbor_returncode"] == 0

"""Offline checks for the optional recovery workflow prompt, not ERP success."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys

import pytest
from test_runner import make_fake_workspace

from forgeflow.agents.pi_nebius import (
    DEFAULT_MODEL,
    NATIVE_RECOVERY_EXTENSION,
    OUTPUT_POLICY_EXTENSION,
    RECOVERY_HELP,
    PiNebius,
    build_pi_command,
)
from forgeflow.config import resolve_execution_guidance
from forgeflow.runner import build_run_command, run_task
from forgeflow.ui import build_snapshot, run_detail

GUIDANCE = "recovery-workflow-v1"
PLANNER = "cp-sat-recovery-v1"
REVIEWED_PROMPT_SHA256 = "99ac2981392e8c157d6e8fa1343bfe6527a6aa176212fc387c9922524fa461b9"


@pytest.mark.parametrize(
    "planner", ["none", "cp-sat-live-v1", "cp-sat-native-v1", "cp-sat-native-v2"],
)
def test_unavailable_recovery_tools_are_rejected_before_any_setup(tmp_path, planner):
    options = {"execution_guidance": GUIDANCE, "planner_profile": planner}
    with pytest.raises(ValueError, match="requires planner_profile='cp-sat-recovery-v1'"):
        build_run_command(
            tmp_path, "A", "pi", tmp_path / "runs/test", model=DEFAULT_MODEL, **options,
        )
    with pytest.raises(ValueError, match="requires planner_profile='cp-sat-recovery-v1'"):
        run_task(tmp_path, "A", "pi", run_id="incompatible", **options)
    with pytest.raises(ValueError, match="requires planner_profile='cp-sat-recovery-v1'"):
        build_pi_command("unchanged task", DEFAULT_MODEL, **options)
    with pytest.raises(ValueError, match="requires planner_profile='cp-sat-recovery-v1'"):
        PiNebius(logs_dir=tmp_path / "logs", workspace_root=tmp_path, **options)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_controls_reject_workflow_before_creating_a_run(tmp_path, agent):
    options = {"execution_guidance": GUIDANCE, "planner_profile": PLANNER}
    with pytest.raises(ValueError, match="only for the Pi agent"):
        build_run_command(tmp_path, "A", agent, tmp_path / "run", **options)
    with pytest.raises(ValueError, match="only for the Pi agent"):
        run_task(tmp_path, "A", agent, run_id="incompatible", **options)
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize("provider_default", [True, False])
def test_real_shell_argv_preserves_task_and_only_adds_reviewed_prompt(
    tmp_path, provider_default,
):
    script = tmp_path / "capture arguments.py"
    script.write_text("import json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    unexpected = tmp_path / "must-not-exist"
    instruction = (
        f"Original task\n'quoted' and $(touch '{unexpected}') "
        f"and `touch '{unexpected}'` and $FORGEFLOW_ARGV_TEST"
    )

    def capture(profile, name):
        log = tmp_path / name
        command = build_pi_command(
            instruction, DEFAULT_MODEL, executable=(sys.executable, str(script)),
            output_file=str(log), execution_guidance=profile, planner_profile=PLANNER,
            provider_default_output=provider_default,
        )
        subprocess.run(
            ["bash", "-c", command], check=True, capture_output=True, timeout=10,
        )
        return json.loads(log.read_text())

    baseline = capture("none", "baseline argv.json")
    arguments = capture(GUIDANCE, "guided argv.json")
    guidance = resolve_execution_guidance(GUIDANCE)
    assert guidance["sha256"] == REVIEWED_PROMPT_SHA256
    assert hashlib.sha256(guidance["text"].encode()).hexdigest() == guidance["sha256"]
    assert guidance["version"] == 1
    assert guidance["instruction_modified"] is False
    assert arguments.count("--append-system-prompt") == 1
    append_index = arguments.index("--append-system-prompt")
    assert append_index < arguments.index("--")
    assert arguments[append_index + 1] == guidance["text"] + "\n\n" + RECOVERY_HELP
    assert arguments[append_index + 1].count(RECOVERY_HELP) == 1
    assert baseline[append_index + 1] == RECOVERY_HELP
    assert (
        arguments[:append_index + 1] + arguments[append_index + 2:]
        == baseline[:append_index + 1] + baseline[append_index + 2:]
    )
    assert arguments[-2:] == ["--", instruction]
    assert NATIVE_RECOVERY_EXTENSION in arguments
    assert (OUTPUT_POLICY_EXTENSION in arguments) is provider_default
    assert not unexpected.exists()


def test_fake_run_records_prompt_and_ui_separates_it_without_budget_changes(
    tmp_path, monkeypatch, synthetic_host_runtime,
):
    # Existing fake Harbor fixture writes synthetic evidence; no Docker/model call.
    make_fake_workspace(tmp_path)
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=fixture-key\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    baseline_dir = run_task(
        tmp_path, "A", "pi", run_id="recovery-unguided", planner_profile=PLANNER,
        thinking_mode="off",
    )
    guided_dir = run_task(
        tmp_path, "A", "pi", run_id="recovery-guided", planner_profile=PLANNER,
        thinking_mode="off", execution_guidance=GUIDANCE,
    )
    baseline = json.loads((baseline_dir / "manifest.json").read_text())
    guided = json.loads((guided_dir / "manifest.json").read_text())
    assert baseline["state"] == guided["state"] == "completed"
    for key in ("budget", "planner", "data_profile", "execution_config_overrides", "model"):
        assert guided[key] == baseline[key]
    assert baseline["execution_guidance"] == resolve_execution_guidance("none")
    assert guided["execution_guidance"] == resolve_execution_guidance(GUIDANCE)
    assert f"execution_guidance={GUIDANCE}" in guided["command"]
    assert not any("execution_guidance=" in part for part in baseline["command"])
    detail = run_detail(tmp_path, guided_dir.name)
    assert detail["execution_guidance"] == {
        "profile": GUIDANCE, "label": "恢复工作流提示", "version": 1,
        "sha256": REVIEWED_PROMPT_SHA256, "recorded": True,
    }
    groups = build_snapshot(tmp_path)["agent_groups"]
    assert len(groups) == 2
    assert {group["execution_guidance"]["profile"] for group in groups} == {"none", GUIDANCE}
    assert all(group["measured_agent_runs"] == 1 for group in groups)

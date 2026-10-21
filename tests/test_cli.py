from pathlib import Path

import pytest

from forgeflow.cli import build_parser, main


@pytest.mark.parametrize(
    ("agent", "summary", "expected_exit"),
    [
        ("nop", {"environment_chain_ok": True, "expected_control_outcome": True}, 0),
        ("oracle", {"environment_chain_ok": False, "strict_success": True}, 1),
        ("pi", {"environment_chain_ok": True, "strict_success": False}, 1),
        ("pi", {"environment_chain_ok": True, "strict_success": True}, 0),
        ("pi", {"state": "interrupted"}, 130),
        ("pi", {}, 1),
    ],
)
def test_run_exit_status_reflects_valid_result(monkeypatch, capsys, agent, summary, expected_exit):
    monkeypatch.setattr("forgeflow.runner.run_task", lambda *args, **kwargs: Path("/tmp/run"))
    monkeypatch.setattr("forgeflow.runner.summarize_run", lambda path: summary)
    assert main(["run", "A", "--agent", agent]) == expected_exit
    assert '"run_dir": "/tmp/run"' in capsys.readouterr().out


def test_report_rejects_nonexistent_run(tmp_path, capsys):
    assert main(["report", str(tmp_path / "missing")]) == 2
    assert "manifest.json" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("arguments", "expected_max_tokens"),
    [([], None), (["--max-tokens", "8192"], 8192)],
)
def test_probe_budget_defaults_to_provider_and_preserves_explicit_override(
    monkeypatch, arguments, expected_max_tokens,
):
    assert build_parser().parse_args(["probe-model"]).max_tokens is None
    calls = []
    monkeypatch.setattr(
        "forgeflow.model_probe.run_model_probe",
        lambda *args, **kwargs: calls.append(kwargs) or {"status": "passed"},
    )
    assert main(["probe-model", *arguments]) == 0
    assert calls[0]["max_tokens"] == expected_max_tokens
    assert calls[0]["timeout_seconds"] == 60


def test_run_output_budget_defaults_to_provider_and_passes_explicit_value(monkeypatch):
    assert build_parser().parse_args(["run", "A", "--agent", "pi"]).max_output_tokens is None
    calls = []
    monkeypatch.setattr(
        "forgeflow.runner.run_task",
        lambda *args, **kwargs: calls.append(kwargs) or Path("/tmp/run"),
    )
    monkeypatch.setattr(
        "forgeflow.runner.summarize_run",
        lambda path: {"environment_chain_ok": True, "strict_success": True},
    )
    assert main(["run", "A", "--agent", "pi", "--max-output-tokens", "32768"]) == 0
    assert calls[0]["max_output_tokens"] == 32768
    assert calls[0]["agent_timeout_sec"] == 3600


def test_run_output_budget_is_pi_only(tmp_path, capsys):
    assert main([
        "--workspace", str(tmp_path), "run", "A",
        "--agent", "oracle", "--max-output-tokens", "32768",
    ]) == 2
    assert "only for the Pi agent" in capsys.readouterr().out
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("mode", ["provider-default", "off"])
def test_thinking_mode_cli_passes_selected_mode_without_changing_other_factors(monkeypatch, mode):
    defaults = build_parser().parse_args(["run", "A", "--agent", "pi"])
    assert defaults.thinking_mode == "provider-default"
    calls = []
    monkeypatch.setattr(
        "forgeflow.runner.run_task",
        lambda *args, **kwargs: calls.append(kwargs) or Path("/tmp/run"),
    )
    monkeypatch.setattr(
        "forgeflow.runner.summarize_run",
        lambda path: {"environment_chain_ok": True, "strict_success": True},
    )
    assert main(["run", "A", "--agent", "pi", "--thinking-mode", mode]) == 0
    assert calls[0]["thinking_mode"] == mode
    assert calls[0]["max_output_tokens"] is None
    assert calls[0]["execution_guidance"] == "none"
    assert calls[0]["planner_profile"] == "none"
    assert calls[0]["agent_timeout_sec"] == 3600


@pytest.mark.parametrize("agent", ["oracle", "nop"])
def test_thinking_off_cli_rejects_controls_before_creating_run(tmp_path, capsys, agent):
    assert main([
        "--workspace", str(tmp_path), "run", "A", "--agent", agent, "--thinking-mode", "off",
    ]) == 2
    assert "only for the Pi agent" in capsys.readouterr().out
    assert not (tmp_path / "runs").exists()


def test_unknown_thinking_mode_cli_rejected_before_runner(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Invalid CLI mode must not reach the runner.")
    monkeypatch.setattr("forgeflow.runner.run_task", unexpected)
    with pytest.raises(SystemExit) as error:
        main(["run", "A", "--agent", "pi", "--thinking-mode", "high"])
    assert error.value.code == 2


@pytest.mark.parametrize("profile", ["odoo-read-v1", "odoo-schedule-v1"])
def test_guidance_defaults_off_and_cli_passes_profile_without_changing_budget(monkeypatch, profile):
    defaults = build_parser().parse_args(["run", "A", "--agent", "pi"])
    assert defaults.execution_guidance == "none"
    calls = []
    monkeypatch.setattr(
        "forgeflow.runner.run_task",
        lambda *args, **kwargs: calls.append(kwargs) or Path("/tmp/run"),
    )
    monkeypatch.setattr(
        "forgeflow.runner.summarize_run",
        lambda path: {"environment_chain_ok": True, "strict_success": True},
    )
    assert main([
        "run", "A", "--agent", "pi", "--execution-guidance", profile,
    ]) == 0
    assert calls[0]["execution_guidance"] == profile
    assert calls[0]["max_output_tokens"] is None
    assert calls[0]["agent_timeout_sec"] == 3600


@pytest.mark.parametrize("agent", ["oracle", "nop", "pi"])
def test_data_profile_is_opt_in_and_available_to_controls_and_pi(monkeypatch, agent):
    assert build_parser().parse_args(["run", "A", "--agent", agent]).data_profile == "original"
    calls = []
    monkeypatch.setattr(
        "forgeflow.runner.run_task",
        lambda *args, **kwargs: calls.append(kwargs) or Path("/tmp/run"),
    )
    monkeypatch.setattr(
        "forgeflow.runner.summarize_run",
        lambda path: {"environment_chain_ok": True, "strict_success": True, "expected_control_outcome": True},
    )
    assert main([
        "run", "A", "--agent", agent, "--data-profile", "planning-metadata-v1",
    ]) == 0
    assert calls[0]["data_profile"] == "planning-metadata-v1"
    assert calls[0]["execution_guidance"] == "none"


@pytest.mark.parametrize("planner_profile", ["cp-sat-live-v1", "cp-sat-native-v1", "cp-sat-native-v2", "cp-sat-recovery-v1"])
def test_planner_cli_defaults_off_and_does_not_enable_other_profiles(monkeypatch, planner_profile):
    defaults = build_parser().parse_args(["run", "A", "--agent", "pi"])
    assert defaults.planner_profile == "none"
    calls = []
    monkeypatch.setattr(
        "forgeflow.runner.run_task",
        lambda *args, **kwargs: calls.append(kwargs) or Path("/tmp/run"),
    )
    monkeypatch.setattr(
        "forgeflow.runner.summarize_run",
        lambda path: {"environment_chain_ok": True, "strict_success": True},
    )
    assert main(["run", "A", "--agent", "pi", "--planner-profile", planner_profile]) == 0
    assert calls[0]["planner_profile"] == planner_profile
    assert calls[0]["data_profile"] == "original"
    assert calls[0]["execution_guidance"] == "none"
    assert calls[0]["max_output_tokens"] is None

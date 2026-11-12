"""Recovery mode is an enforced execution boundary, not a prompt preference."""
import base64
import hashlib
import json
import shlex
import shutil
import subprocess
from pathlib import Path

import pytest

from forgeflow.config import validate_recovery_mode
from forgeflow.runner import _read_investigation_event, build_run_command, run_task

PROFILES = {"planner_profile": "cp-sat-recovery-v1", "security_profile": "permission-split-v1"}


def event_payload(text="The supplier cancelled the incoming delivery.\n"):
    return {"text": text, "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def partial_policy():
    return {
        "kind": "forgeflow.investigation_policy", "schema_version": 1,
        "policy_id": "intake-1", "task_id": "2280_medium_repair_plan_medium",
        "task_type": "supplier_cancellation", "anchor_date": "2026-10-22",
        "margin": 0.25, "margin_basis": "sales revenue", "time_granularity": "day",
        "orders": {"1": {"authorized": True, "budget_cap": None}},
        "sources": {"authorization": "Reviewed order scope; budget requires clarification."},
    }


@pytest.mark.parametrize("mode", ["prepare-only", "investigation"])
@pytest.mark.parametrize("override", [
    {"agent": "oracle"}, {"planner_profile": "none"}, {"security_profile": "none"},
    {"execution_guidance": "recovery-workflow-v1"}, {"execution_guidance": "odoo-schedule-v1"},
    {"contract_guidance": "reviewed-request-v1"},
])
def test_mode_mismatch_fails_before_creating_run(tmp_path, mode, override):
    args = {"agent": "pi", **PROFILES, **override}
    with pytest.raises(ValueError, match="recovery_mode"):
        validate_recovery_mode(mode, **args)
    agent = args.pop("agent")
    with pytest.raises(ValueError, match="recovery_mode"):
        run_task(tmp_path, "A", agent, recovery_mode=mode, **args)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("mode", [None, {}, "prepare", ""])
def test_unknown_recovery_mode_is_rejected(mode):
    with pytest.raises(ValueError, match="recovery_mode"):
        validate_recovery_mode(mode, agent="pi", **PROFILES)


@pytest.mark.parametrize("mode", ["prepare-only", "investigation"])
def test_mode_reaches_harbor_and_selects_only_the_correct_pi_extension(tmp_path, mode):
    from forgeflow.agents.pi_nebius import (
        DEFAULT_MODEL,
        NATIVE_INVESTIGATION_EXTENSION,
        NATIVE_RECOVERY_EXTENSION,
        build_pi_command,
    )
    command = build_run_command(tmp_path, "A", "pi", tmp_path / "run", model=DEFAULT_MODEL,
                                recovery_mode=mode, **PROFILES)
    assert f"recovery_mode={mode}" in command
    invocation = shlex.split(build_pi_command("public event", DEFAULT_MODEL, recovery_mode=mode, **PROFILES))
    extensions = [invocation[i + 1] for i, arg in enumerate(invocation) if arg == "-e"]
    selected = NATIVE_INVESTIGATION_EXTENSION if mode == "investigation" else NATIVE_RECOVERY_EXTENSION
    assert selected in extensions
    assert (NATIVE_RECOVERY_EXTENSION in extensions) == (mode == "prepare-only")
    assert ("--tools" in invocation) == (mode == "investigation")
    if mode == "investigation":
        assert invocation[invocation.index("--tools") + 1].split(",") == [
            "investigation_query", "validate_investigation_draft", "prepare_investigated_recovery",
            "investigation_status", "finish_investigation",
        ]
    guidance = invocation[invocation.index("--append-system-prompt") + 1]
    assert "execute_recovery" not in guidance
    assert invocation[invocation.index("--") + 1] == "public event"


@pytest.mark.parametrize("mode", ["autonomous", "prepare-only", "investigation"])
def test_executor_launch_preserves_only_the_selected_authority(tmp_path, monkeypatch, mode):
    from test_contract import supplier_contract

    from forgeflow.agents.pi_nebius import PiNebius

    monkeypatch.setenv("FORGEFLOW_CONTRACT_JSON", json.dumps(supplier_contract().payload()))
    event = event_payload("供应商取消了供货。\n$(literal) 'quotes'")
    monkeypatch.setenv("FORGEFLOW_INVESTIGATION_EVENT_JSON", json.dumps(event))
    agent = PiNebius(logs_dir=tmp_path / "logs", recovery_mode=mode, **PROFILES)
    start = agent._executor_start_command()
    assert ("--autonomous" in start) == (mode == "autonomous")
    assert ("--investigation" in start) == (mode == "investigation")
    assert ("--investigation-event-file" in start) == (mode == "investigation")
    if mode == "investigation":
        assert base64.b64encode(event["text"].encode()).decode() in start
        assert "chmod 0400 /var/lib/forgeflow/recovery/event.txt" in start
    checked = subprocess.run(["bash", "-n", "-c", start], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stderr


def test_investigation_requires_the_trusted_contract_before_agent_setup(tmp_path, monkeypatch):
    from forgeflow.agents.pi_nebius import PiNebius
    monkeypatch.delenv("FORGEFLOW_CONTRACT_JSON", raising=False)
    with pytest.raises(ValueError, match="reviewed task contract"):
        PiNebius(logs_dir=tmp_path / "logs", recovery_mode="investigation", **PROFILES)
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("event", [None, {}, {"text": "event", "sha256": "f" * 64},
                                   event_payload(" "), event_payload("x" * (16 * 1024 + 1))])
def test_investigation_rejects_missing_or_invalid_trusted_event(tmp_path, monkeypatch, event):
    from test_contract import supplier_contract

    from forgeflow.agents.pi_nebius import PiNebius

    monkeypatch.setenv("FORGEFLOW_CONTRACT_JSON", json.dumps(supplier_contract().payload()))
    monkeypatch.setenv("FORGEFLOW_INVESTIGATION_EVENT_JSON", json.dumps(event))
    with pytest.raises(ValueError, match="event"):
        PiNebius(logs_dir=tmp_path / "logs", recovery_mode="investigation", **PROFILES)
    assert not (tmp_path / "logs").exists()


@pytest.mark.parametrize("mode", ["autonomous", "prepare-only", "investigation"])
def test_partial_policy_only_enters_investigation_runtime(tmp_path, monkeypatch, mode):
    from forgeflow.agents.pi_nebius import PiNebius
    from forgeflow.contract import ContractError

    payload = partial_policy()
    monkeypatch.setenv("FORGEFLOW_CONTRACT_JSON", json.dumps(payload))
    monkeypatch.setenv("FORGEFLOW_INVESTIGATION_EVENT_JSON", json.dumps(event_payload()))
    if mode == "investigation":
        agent = PiNebius(logs_dir=tmp_path / "logs", recovery_mode=mode, **PROFILES)
        assert agent.contract_payload == payload
    else:
        with pytest.raises(ContractError, match="not a task contract"):
            PiNebius(logs_dir=tmp_path / "logs", recovery_mode=mode, **PROFILES)
        assert not (tmp_path / "logs").exists()


def test_runner_rejects_partial_policy_for_prepare_only_before_creating_run(tmp_path):
    from forgeflow.contract import ContractError

    path = tmp_path / "policy.json"
    path.write_text(json.dumps(partial_policy()))
    with pytest.raises(ContractError, match="not a task contract"):
        run_task(tmp_path, "A", "pi", recovery_mode="prepare-only", contract_path=path, **PROFILES)
    assert not (tmp_path / "runs").exists()


def test_investigation_requires_event_before_reading_contract_or_creating_run(tmp_path):
    with pytest.raises(ValueError, match="event file"):
        run_task(tmp_path, "A", "pi", recovery_mode="investigation",
                 contract_path=tmp_path / "not-read.json", **PROFILES)
    assert not (tmp_path / "runs").exists()


@pytest.mark.parametrize("content", [b"", b" \n", b"x" * (16 * 1024 + 1), b"\xff", b"event\x00"])
def test_invalid_event_files_fail_before_creating_run(tmp_path, content):
    path = tmp_path / "event.txt"
    path.write_bytes(content)
    with pytest.raises(ValueError, match="event"):
        run_task(tmp_path, "A", "pi", recovery_mode="prepare-only", investigation_event_file=path, **PROFILES)
    assert not (tmp_path / "runs").exists()


def test_event_reader_preserves_exact_utf8_content(tmp_path):
    path = tmp_path / "event.txt"
    event = "供应商取消了供货，请查明影响。\n"
    path.write_text(event, encoding="utf-8")
    assert _read_investigation_event(path) == event


@pytest.mark.usefixtures("synthetic_host_runtime")
@pytest.mark.parametrize(("mode", "partial"), [("prepare-only", False), ("investigation", False), ("investigation", True)])
def test_runner_records_mode_and_passes_only_public_event_to_harbor(tmp_path, monkeypatch, mode, partial):
    from test_contract import supplier_contract
    from test_runner import make_fake_workspace

    executable = make_fake_workspace(tmp_path)
    task_name = "2280_medium_repair_plan_medium"
    target = tmp_path / "vendor/erp-bench/tasks" / task_name
    shutil.rmtree(target)
    shutil.copytree(Path(__file__).resolve().parents[1] / "vendor/erp-bench/tasks" / task_name, target)
    executable.write_text(executable.read_text() +
        "(root/'public-input.json').write_text(json.dumps({'event':(Path(sys.argv[sys.argv.index('-p')+1])/'instruction.md').read_text(),"
        "'run_id':os.environ.get('FORGEFLOW_RUN_ID'),'trusted_event':json.loads(os.environ['FORGEFLOW_INVESTIGATION_EVENT_JSON'])}))\n")
    (tmp_path / ".env").write_text("NEBIUS_API_KEY=fixture-key\nNEBIUS_MODEL=nvidia/example\n")
    for name in ("NEBIUS_API_KEY", "NEBIUS_MODEL", "NEBIUS_BASE_URL"):
        monkeypatch.delenv(name, raising=False)
    contract_path = tmp_path / "contract.json"
    payload = partial_policy() if partial else supplier_contract().payload()
    contract_path.write_text(json.dumps(payload))
    event_path = tmp_path / "private-path-event.txt"
    event_path.write_text("The supplier cancelled the incoming delivery. Investigate and prepare.\n")
    run_dir = run_task(tmp_path, "A", "pi", run_id="episode-public", recovery_mode=mode,
                       investigation_event_file=event_path, contract_path=contract_path, **PROFILES)
    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["state"] == "completed", manifest.get("error")
    assert manifest["recovery_mode"] == mode
    assert manifest["planner"]["read_only"] is True
    assert manifest["planner"]["execution"] == "disabled"
    assert manifest["budget"]["total_tokens"]["max_total_tokens"] == 2000000
    assert "max_total_tokens=2000000" in manifest["command"]
    assert f"recovery_mode={mode}" in manifest["command"]
    assert str(event_path) not in json.dumps(manifest)
    child = json.loads((run_dir / "harbor/job/2280_trial/public-input.json").read_text())
    assert child == {"event": event_path.read_text(), "run_id": "episode-public",
                     "trusted_event": event_payload(event_path.read_text())}
    assert manifest["contract"]["kind"] == payload["kind"]
    # The fake Harbor has no prepared artifact; its 100 ERP reward cannot
    # fabricate success at the preparation stage.
    assert manifest["preparation_result"]["preparation_success"] is False

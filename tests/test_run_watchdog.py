import json
import os
import signal
import subprocess
import sys
from pathlib import Path

import pytest

from forgeflow.budget import BUDGET_MECHANISM_VERSION, BudgetClock
from forgeflow.run_watchdog import RunWatchdog


class Clock:
    def __init__(self):
        self.value = 100.0
        self.utc = 1000.0

    def clock(self):
        return BudgetClock("test-continuous", lambda: self.value, lambda: self.value,
                           lambda: self.utc, lambda: 0.0)


def write_phase(root, clock, phases):
    path = root / "harbor/job/trial/host-budget.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({
        "schema": 1, "mechanism": BUDGET_MECHANISM_VERSION,
        "clock": {"source": "test-continuous", "includes_suspend": True},
        "phases": phases,
    }))


def phase(start, *, finish=None):
    return {"started": {"continuous_sec": start},
            "finished": None if finish is None else {"continuous_sec": finish},
            "status": "running" if finish is None else "completed"}


def test_utc_jump_cannot_extend_continuous_deadline(tmp_path):
    clock = Clock()
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, clock=clock.clock())
    write_phase(tmp_path, clock, {"agent": phase(100)})
    assert watchdog.check() is None
    clock.value = 731
    clock.utc = 2
    assert watchdog.check() == "agent_outer_timeout"
    assert watchdog.report["possible_partial_execution"] is True


def test_cleanup_and_verification_are_separate_from_agent_budget(tmp_path):
    clock = Clock()
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, clock=clock.clock())
    clock.value = 705
    records = {"agent": phase(100, finish=700), "cleanup": phase(700)}
    write_phase(tmp_path, clock, records)
    assert watchdog.check() is None
    records["cleanup"] = phase(700, finish=710)
    clock.value = 800
    write_phase(tmp_path, clock, records)
    assert watchdog.check() is None
    assert watchdog.report["phase"] == "verification_and_finalize"
    clock.value = 1071
    assert watchdog.check() == "post_agent_timeout"


@pytest.mark.parametrize("change", ["restart", "reopen", "removed", "finish", "symlink"])
def test_phase_changes_cannot_extend_budget(tmp_path, change):
    clock = Clock()
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, clock=clock.clock())
    clock.value = 110
    records = {"agent": phase(100, finish=105), "cleanup": phase(105)}
    write_phase(tmp_path, clock, records)
    assert watchdog.check() is None
    if change == "restart":
        records["cleanup"] = phase(106)
    elif change == "reopen":
        records["agent"] = phase(100)
    elif change == "removed":
        records.pop("agent")
    elif change == "finish":
        records["agent"] = phase(100, finish=106)
    else:
        path = tmp_path / "harbor/job/trial/host-budget.json"
        other = tmp_path / "outside.json"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    if change != "symlink":
        write_phase(tmp_path, clock, records)
    with pytest.raises(ValueError):
        watchdog.check()


def test_independent_watchdog_stops_real_silent_child_without_waiting_for_stdout(tmp_path):
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, environment_start_sec=0.15)
    process = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(20)"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )

    def terminate(child):
        child.send_signal(signal.SIGTERM)
        child.wait(timeout=3)
        return []

    try:
        watchdog.start(process, terminate)
        assert process.wait(timeout=4) != 0
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        report = watchdog.stop()
        process.stdout.close()
        process.stderr.close()
    assert report["status"] == "terminated"
    assert report["reason"] == "environment_start_timeout"
    assert report["possible_partial_execution"] is True
    assert Path(tmp_path / "host-watchdog.json").exists()


@pytest.mark.skipif(not hasattr(os, "fork"), reason="POSIX process-group supervision")
def test_exited_leader_cannot_leave_descendant_holding_output_forever(tmp_path):
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, environment_start_sec=0.15)
    process = subprocess.Popen(
        [sys.executable, "-c", "import os,time; child=os.fork(); time.sleep(20) if child==0 else None"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
    )

    def terminate(child):
        os.killpg(child.pid, signal.SIGTERM)
        child.wait(timeout=3)
        return []

    try:
        watchdog.start(process, terminate)
        process.communicate(timeout=4)
    finally:
        if watchdog.report["status"] != "terminated":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        process.wait(timeout=3)
        report = watchdog.stop()
    assert process.returncode == 0
    assert report["status"] == "terminated"
    assert report["reason"] == "environment_start_timeout"


def test_actual_verifier_stage_has_its_own_deadline_and_cannot_extend_total(tmp_path):
    clock = Clock()
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, clock=clock.clock())
    clock.value = 720
    write_phase(tmp_path, clock, {'agent': phase(100, finish=700), 'cleanup': phase(700, finish=710)})
    root = tmp_path / 'harbor/job/trial'
    payload = json.loads((root / 'host-budget.json').read_text())
    payload['phases'] = {'verification': phase(720)}
    path = root / 'host-verifier-budget.json'
    path.write_text(json.dumps(payload))
    assert watchdog.check() is None
    assert watchdog.report['phase'] == 'verification'
    clock.value = 1020
    assert watchdog.check() == 'verification_outer_timeout'
    payload['phases']['verification'] = phase(720, finish=1010)
    path.write_text(json.dumps(payload))
    assert watchdog.check() is None
    assert watchdog.report['phase'] == 'finalize'
    clock.value = 1070
    assert watchdog.check() == 'post_agent_timeout'


@pytest.mark.parametrize("earlier", ["setup", "agent", "cleanup"])
def test_verifier_cannot_overlap_active_agent(tmp_path, earlier):
    clock = Clock()
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, clock=clock.clock())
    write_phase(tmp_path, clock, {earlier: phase(100)})
    path = tmp_path / 'harbor/job/trial/host-verifier-budget.json'
    payload = json.loads(path.with_name('host-budget.json').read_text())
    payload['phases'] = {'verification': phase(100)}
    path.write_text(json.dumps(payload))
    with pytest.raises(ValueError, match=f'overlapped {earlier}'):
        watchdog.check()


def test_verifier_cannot_hide_missing_agent_phase_record(tmp_path):
    clock = Clock()
    watchdog = RunWatchdog(tmp_path, agent_sec=600, verifier_sec=300, clock=clock.clock())
    write_phase(tmp_path, clock, {"verification": phase(100)})
    original = tmp_path / "harbor/job/trial/host-budget.json"
    original.rename(original.with_name("host-verifier-budget.json"))
    with pytest.raises(ValueError, match="missing its prior"):
        watchdog.check()

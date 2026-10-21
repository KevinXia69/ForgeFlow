"""Observer diagnostics cannot turn a sleep request, stall or reboot into a pass."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from forgeflow.budget import BudgetClock

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "verify_suspend_budget.py"
SPEC = importlib.util.spec_from_file_location("suspend_probe", SCRIPT)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)


@pytest.fixture
def controlled(monkeypatch):
    values = {"continuous": 100.0, "monotonic": 100.0, "utc": 1_800_000_000.0}
    clock = BudgetClock(
        "darwin-mach-continuous-time", lambda: values["continuous"],
        lambda: values["monotonic"], lambda: values["utc"], lambda: 0.01,
    )
    monkeypatch.setattr(probe, "system_clock", lambda: clock)
    monkeypatch.setattr(probe, "process_identity", lambda pid: "controlled start")
    monkeypatch.setattr(probe, "host_snapshot", lambda: {"boot_utc_sec": 1.0, "sleep_count": 0})
    monkeypatch.setattr(probe, "power_diagnostics", lambda started: {"events": [
        {"kind": "software_sleep_requested"}, {"kind": "cpu_assertion_prevented_sleep"},
    ]})
    return values


@pytest.mark.parametrize("utc_delta", [0.2, 7200.0, -7200.0])
def test_requested_but_prevented_sleep_and_wall_jump_never_pass(controlled, monkeypatch, tmp_path, utc_delta):
    def advance(seconds):
        controlled["continuous"] += 0.2
        controlled["monotonic"] += 0.2
        controlled["utc"] += utc_delta

    monkeypatch.setattr(probe.time, "sleep", advance)
    report = probe.observe(tmp_path / "probe.json", 0.3)
    assert report["status"] == "sleep_not_observed"
    assert "new_dispatch_refused" not in report
    assert report["power_diagnostics"]["events"][0]["kind"] == "software_sleep_requested"


def test_scheduler_stall_is_not_a_sleep(controlled, monkeypatch, tmp_path):
    def advance(seconds):
        controlled["continuous"] += 20
        controlled["monotonic"] += 20
        controlled["utc"] += 20

    monkeypatch.setattr(probe.time, "sleep", advance)
    assert probe.observe(tmp_path / "probe.json", 10)["status"] == "sleep_not_observed"


def test_measured_sleep_uses_real_expiry_dispatch_gate(controlled, monkeypatch, tmp_path):
    def advance(seconds):
        controlled["continuous"] += 10.2
        controlled["monotonic"] += 0.2
        controlled["utc"] += 10.2

    monkeypatch.setattr(probe.time, "sleep", advance)
    report = probe.observe(tmp_path / "probe.json", 30)
    assert report["status"] == "passed"
    assert report["observed_sleep_gap_sec"] == pytest.approx(10)
    assert report["remaining_after_wake_sec"] == 0
    assert report["new_dispatch_refused"] is True
    assert report["model_called"] is False


def test_sleep_during_heartbeat_write_is_also_measured(controlled, monkeypatch, tmp_path):
    def advance(seconds):
        controlled["continuous"] += 0.2
        controlled["monotonic"] += 0.2
        controlled["utc"] += 0.2

    writes = 0

    def sync(fd):
        nonlocal writes
        writes += 1
        if writes == 2:
            controlled["continuous"] += 12
            controlled["utc"] += 12

    monkeypatch.setattr(probe.time, "sleep", advance)
    monkeypatch.setattr(probe.os, "fsync", sync)
    report = probe.observe(tmp_path / "probe.json", 10)
    assert report["status"] == "passed"
    assert report["observed_sleep_gap_sec"] == pytest.approx(12)
    assert report["new_dispatch_refused"] is True


def test_power_events_keep_fixed_labels_only():
    raw = "\n".join([
        "2026-10-23 17:07:11.372 Df kernel[0:7f] PMRD: sleep reason Software Sleep",
        "2026-10-23 17:07:11.534 Df kernel[0:7f] PMRD: System sleep prevented by kPMCPUAssertion",
        "2026-10-23 17:07:11.722 Df kernel[0:7f] PMRD: Requesting full wake due to dark wake activity tickle",
        "2026-10-23 17:07:11.900 Df another-process unrelated secret data",
    ])
    events = probe.parse_power_events(raw)
    assert [event["kind"] for event in events] == [
        "software_sleep_requested", "cpu_assertion_prevented_sleep", "activity_requested_full_wake",
    ]
    assert "secret" not in json.dumps(events)
    assert all(set(event) == {"local_time", "kind"} for event in events)


@pytest.mark.parametrize(("boot", "identity", "expected"), [
    (2000, "original", "interrupted_by_host_restart"),
    (1, None, "process_missing_or_replaced"),
    (1, "another process", "process_missing_or_replaced"),
    (1, "original", "running"),
])
def test_inspect_distinguishes_restart_pid_reuse_and_live_process(monkeypatch, tmp_path, boot, identity, expected):
    path = tmp_path / "probe.json"
    path.write_text(json.dumps({"status": "observing", "started": {"utc_sec": 1000},
                                "observer": {"pid": 42, "process_start": "original"}}))
    original = path.read_bytes()
    monkeypatch.setattr(probe, "host_snapshot", lambda: {"boot_utc_sec": boot})
    monkeypatch.setattr(probe, "process_identity", lambda pid: identity)
    monkeypatch.setattr(probe, "power_diagnostics", lambda start: {"events": []})
    report = probe.inspect_report(path)
    assert report["observer_liveness"] == expected
    assert report["acceptance"] == "unverified"
    assert path.read_bytes() == original


def test_legacy_observing_after_reboot_is_not_reported_as_live(monkeypatch, tmp_path):
    path = tmp_path / "probe.json"
    path.write_text(json.dumps({"status": "observing", "started": {"utc_sec": 1000}}))
    monkeypatch.setattr(probe, "host_snapshot", lambda: {"boot_utc_sec": 2000})
    monkeypatch.setattr(probe, "power_diagnostics", lambda start: {"events": []})
    assert probe.inspect_report(path)["observer_liveness"] == "interrupted_by_host_restart"


def test_query_failure_is_unknown_not_empty_success(monkeypatch):
    monkeypatch.setattr(probe.sys, "platform", "darwin")
    monkeypatch.setattr(probe, "read_command", lambda *args, **kwargs: None)
    assert probe.power_diagnostics(time.time() - 60)["status"] == "unavailable"
    assert probe.host_snapshot() == {"boot_utc_sec": None, "sleep_count": None, "last_sleep_utc_sec": None}


@pytest.mark.skipif(sys.platform != "darwin", reason="Actual macOS observer process")
def test_actual_process_persists_heartbeat_and_term_without_claiming_sleep(tmp_path):
    path = tmp_path / "probe.json"
    process = subprocess.Popen([
        sys.executable, str(SCRIPT), "--out", str(path), "--observe-sec", "20",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        end = time.monotonic() + 8
        while time.monotonic() < end:
            if path.exists():
                report = json.loads(path.read_text())
                if report["heartbeat"]["continuous_sec"] > report["started"]["continuous_sec"]:
                    break
            time.sleep(0.05)
        else:
            pytest.fail("observer did not persist a heartbeat")
        assert report["observer"]["pid"] == process.pid
        assert probe.inspect_report(path)["observer_liveness"] == "running"
        process.terminate()
        process.wait(timeout=15)
        assert process.returncode == 2
        final = json.loads(path.read_text())
        assert final["status"] == "interrupted"
        assert final["sample_count"] > 0
        assert "finished" in final
        assert "new_dispatch_refused" not in final
        assert probe.inspect_report(path)["observer_liveness"] == "process_missing_or_replaced"
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=5)

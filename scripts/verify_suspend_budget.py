"""Observe a manually initiated macOS sleep; never suspends the user's computer.

No Docker, ERP or model is started. A rolling, independent two-second probe
deadline is used only for measurement, never to extend a real run's budget.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from forgeflow.budget import BudgetExceeded, Deadline, bounded_await, system_clock

EVENTS = {
    "PMRD: sleep reason Software Sleep": "software_sleep_requested",
    "PMRD: System sleep prevented by kPMCPUAssertion": "cpu_assertion_prevented_sleep",
    "PMRD: Requesting full wake due to dark wake activity tickle": "activity_requested_full_wake",
    "Display state: Off NotificationWake": "display_off",
    "Display state: On NotificationWake": "display_on",
}


def read_command(argv: list[str], timeout: float = 3) -> str | None:
    """Read only; never retain unrelated logs, command stderr or exception text."""
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout if result.returncode == 0 else None


def host_snapshot() -> dict:
    result = {"boot_utc_sec": None, "sleep_count": None, "last_sleep_utc_sec": None}
    if sys.platform != "darwin":
        return result
    for key, name in (("boot_utc_sec", "kern.boottime"), ("last_sleep_utc_sec", "kern.sleeptime")):
        value = read_command(["/usr/sbin/sysctl", "-n", name])
        match = re.search(r"sec = (\d+), usec = (\d+)", value or "")
        if match:
            result[key] = int(match[1]) + int(match[2]) / 1_000_000
    counts = read_command(["/usr/bin/pmset", "-g", "stats"])
    match = re.search(r"^Sleep Count:(\d+)$", counts or "", re.MULTILINE)
    if match:
        result["sleep_count"] = int(match[1])
    return result


def parse_power_events(raw: str) -> list[dict]:
    """Keep only fixed event labels and timestamps; never copy raw system logs."""
    events = []
    for line in raw.splitlines():
        if not re.match(r"^\d{4}-\d\d-\d\d \d\d:\d\d:\d\d\.\d{3} ", line):
            continue
        for marker, kind in EVENTS.items():
            if marker in line:
                events.append({"local_time": line[:23], "kind": kind})
                break
    return events


def power_diagnostics(start_utc_sec: float) -> dict:
    result = {"source": "macOS unified kernel/powerd log", "status": "unavailable", "events": []}
    if sys.platform != "darwin":
        return result
    # A bounded historical query also works after a detached observer dies/reboots.
    # Unknown/error is explicit. Sleep-request notifications are NEVER a pass.
    now = time.time()
    beginning = max(start_utc_sec, now - 3600)
    result["window_limited"] = beginning != start_utc_sec
    result["timezone"] = datetime.now().astimezone().tzname()
    predicate = '(process == "kernel" OR process == "powerd") AND (' + " OR ".join(
        f'eventMessage CONTAINS "{marker}"' for marker in EVENTS
    ) + ")"
    raw = read_command([
        "/usr/bin/log", "show", "--style", "compact", "--start",
        datetime.fromtimestamp(beginning).strftime("%Y-%m-%d %H:%M:%S"),
        "--end", datetime.fromtimestamp(now).strftime("%Y-%m-%d %H:%M:%S"),
        "--predicate", predicate,
    ], timeout=8)
    if raw is not None:
        events = parse_power_events(raw)
        result.update(status="available", events=events[-40:], truncated=len(events) > 40)
    return result


def process_identity(pid: int) -> str | None:
    value = read_command(["/bin/ps", "-p", str(pid), "-o", "lstart="])
    return value.strip() if value and value.strip() else None


def inspect_report(output: Path) -> dict:
    """Diagnose a saved report without changing it or restarting the observation."""
    report = json.loads(output.read_text())
    host = host_snapshot()
    started = report["started"]["utc_sec"]
    observer = report.get("observer", {})
    pid = observer.get("pid")
    identity = process_identity(pid) if isinstance(pid, int) and pid > 0 else None
    rebooted = host["boot_utc_sec"] is not None and host["boot_utc_sec"] > started
    if rebooted:
        liveness = "interrupted_by_host_restart"
    elif observer.get("process_start") and identity == observer["process_start"]:
        liveness = "running"
    elif observer.get("process_start"):
        liveness = "process_missing_or_replaced"
    else:
        liveness = "unknown_legacy_report"
    heartbeat = report.get("heartbeat", report["started"])
    diagnostics = power_diagnostics(started)
    kinds = {event["kind"] for event in diagnostics["events"]}
    return {
        "saved_status": report["status"], "observer_liveness": liveness,
        "heartbeat_age_sec": time.time() - heartbeat["utc_sec"],
        "host_now": host, "power_diagnostics": diagnostics,
        "sleep_request_observed": "software_sleep_requested" in kinds,
        "sleep_prevention_observed": "cpu_assertion_prevented_sleep" in kinds,
        "acceptance": "passed" if report["status"] == "passed" else "unverified",
    }


class ObservationInterrupted(Exception):
    pass


async def dispatch_is_refused(deadline: Deadline) -> bool:
    called = False

    async def operation():
        nonlocal called
        called = True

    try:
        await bounded_await(operation, deadline)
    except BudgetExceeded:
        return not called
    return False


def observe(output: Path, seconds: float) -> dict:
    if output.exists():
        raise ValueError("Do not overwrite prior sleep evidence.")
    output.parent.mkdir(parents=True, exist_ok=True)
    clock = system_clock()
    started = clock.capture()
    report = {"scope": "actual_host_sleep_clock_and_post_expiry_dispatch_gate",
              "clock": clock.source, "status": "observing", "started": asdict(started),
              "schema_version": 2, "heartbeat": asdict(started), "sample_count": 0,
              "max_observed_sleep_gap_sec": 0.0,
              "observer": {"pid": os.getpid(), "process_start": process_identity(os.getpid())},
              "host_at_start": host_snapshot(), "observe_limit_sec": seconds,
              "model_called": False, "erp_started": False, "computer_suspended_by_script": False}

    def save():
        temporary = output.with_suffix(".tmp")
        with temporary.open("w") as stream:
            stream.write(json.dumps(report, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(output)

    if clock.source != "darwin-mach-continuous-time":
        report["status"] = "unsupported_probe_clock"
        save()
        return report
    save()
    deadline = Deadline(clock, 2)
    print("Observer ready. Manually sleep the Mac for at least 10 seconds, then wake it.", flush=True)
    try:
        while True:
            before = deadline.started
            time.sleep(0.2)
            # Consecutive deadline samples cover file writes/scheduling too; a
            # sleep between loop iterations must not disappear from measurement.
            next_deadline = Deadline(clock, 2)
            after = next_deadline.started
            continuous = after.continuous_sec - before.continuous_sec
            monotonic = after.monotonic_sec - before.monotonic_sec
            sleep_gap = continuous - monotonic
            report["sample_count"] += 1
            report["max_observed_sleep_gap_sec"] = max(report["max_observed_sleep_gap_sec"], sleep_gap)
            if sleep_gap >= 3:
                report.update(
                    before_sleep=asdict(before), after_wake=asdict(after),
                    continuous_elapsed_sec=continuous, ordinary_monotonic_elapsed_sec=monotonic,
                    observed_sleep_gap_sec=sleep_gap, utc_elapsed_sec=after.utc_sec - before.utc_sec,
                    probe_budget_sec=deadline.limit_sec, remaining_after_wake_sec=deadline.remaining(),
                    new_dispatch_refused=asyncio.run(dispatch_is_refused(deadline)),
                )
                report["status"] = "passed" if report["new_dispatch_refused"] else "failed"
                break
            if after.continuous_sec - started.continuous_sec >= seconds:
                report["status"] = "sleep_not_observed"
                break
            deadline = next_deadline
            if after.continuous_sec - report["heartbeat"]["continuous_sec"] >= 2:
                report["heartbeat"] = asdict(after)
                save()
    except ObservationInterrupted:
        report["status"] = "interrupted"
    except Exception as exc:
        report.update(status="observer_error", error_kind=type(exc).__name__)
    try:
        report["finished"] = asdict(clock.capture())
        report["heartbeat"] = report["finished"]
    except Exception as exc:
        report.update(status="observer_error", error_kind=type(exc).__name__, finished=None)
    # Persist the outcome before optional OS diagnostics, which may be unavailable.
    save()
    report["host_at_finish"] = host_snapshot()
    report["power_diagnostics"] = power_diagnostics(started.utc_sec)
    save()
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--out", type=Path)
    mode.add_argument("--inspect", type=Path, help="Read status, process identity and power events; no writes")
    parser.add_argument("--observe-sec", type=float, default=900)
    args = parser.parse_args()
    if args.inspect:
        print(json.dumps(inspect_report(args.inspect.resolve()), indent=2))
        raise SystemExit(0)
    if not 0.1 <= args.observe_sec <= 3600:
        parser.error("observation must be between 0.1 and 3600 seconds")

    def interrupted(signum, frame):
        raise ObservationInterrupted()

    signal.signal(signal.SIGTERM, interrupted)
    signal.signal(signal.SIGINT, interrupted)
    result = observe(args.out.resolve(), args.observe_sec)
    print(json.dumps({"status": result["status"], "clock": result["clock"]}))
    raise SystemExit(0 if result["status"] == "passed" else 2)

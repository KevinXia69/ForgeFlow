"""Independent host supervision of a Harbor child, including blocked stdout.

Killing the host process never proves an in-flight ERP operation was rolled back.
Only safe phase timestamps are consumed; no agent prompts or exception text.
"""

from __future__ import annotations

import json
import math
import threading
from dataclasses import asdict
from pathlib import Path

from forgeflow.budget import (
    BUDGET_MECHANISM_VERSION,
    CLEANUP_ALLOWANCE_SEC,
    SETUP_BUDGET_SEC,
    BudgetClock,
    positive_seconds,
    system_clock,
)

ENVIRONMENT_START_ALLOWANCE_SEC = 1800.0
TRANSITION_ALLOWANCE_SEC = 60.0
POLL_SEC = 0.1


class RunWatchdog:
    def __init__(
        self, run_dir: Path, *, agent_sec: float, verifier_sec: float,
        clock: BudgetClock | None = None,
        environment_start_sec: float = ENVIRONMENT_START_ALLOWANCE_SEC,
    ):
        self.run_dir = run_dir
        self.clock = clock or system_clock()
        self.started = self.clock.capture()
        self.latest = self.started.continuous_sec
        self.initial_deadline = self.latest + positive_seconds(environment_start_sec)
        self.limits = {
            "setup": SETUP_BUDGET_SEC, "agent": positive_seconds(agent_sec),
            "cleanup": CLEANUP_ALLOWANCE_SEC,
            "verification": positive_seconds(verifier_sec),
        }
        self.post_agent_sec = positive_seconds(verifier_sec) + TRANSITION_ALLOWANCE_SEC
        self.first_starts: dict[str, float] = {}
        self.first_finishes: dict[str, float] = {}
        self.report = {
            "schema": 1, "mechanism": BUDGET_MECHANISM_VERSION,
            "clock": self.clock.source, "started": asdict(self.started),
            "status": "running", "phase": "environment_start",
            "possible_partial_execution": False,
            "environment_start_limit_sec": environment_start_sec,
            "post_agent_limit_sec": self.post_agent_sec,
        }
        self._done = threading.Event()
        self._thread: threading.Thread | None = None

    def _record(self) -> None:
        path = self.run_dir / "host-watchdog.json"
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, sort_keys=True, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(path)

    def _read_phases(self, filename: str) -> dict | None:
        paths = list((self.run_dir / "harbor/job").glob(f"*/{filename}"))
        if not paths:
            return None
        if len(paths) != 1:
            raise ValueError("Ambiguous budget phase records.")
        path = paths[0]
        if (
            path.is_symlink() or any(parent.is_symlink() for parent in path.parents)
            or not path.is_file() or path.stat().st_size > 128 * 1024
        ):
            raise ValueError("Invalid budget phase record.")
        payload = json.loads(path.read_text())
        if (
            payload.get("schema") != 1 or payload.get("mechanism") != BUDGET_MECHANISM_VERSION
            or payload.get("clock", {}).get("source") != self.clock.source
            or payload.get("clock", {}).get("includes_suspend") is not True
        ):
            raise ValueError("Budget clock identity mismatch.")
        return payload

    def _phase_record(self) -> dict | None:
        payload = self._read_phases("host-budget.json")
        verifier = self._read_phases("host-verifier-budget.json")
        if verifier is None:
            return payload
        if set(verifier.get("phases", {})) != {"verification"}:
            raise ValueError("Invalid verifier phase evidence.")
        if payload is None:
            raise ValueError("Verification is missing its prior agent phase evidence.")
        if "verification" in payload.get("phases", {}):
            raise ValueError("Verification cannot be declared in agent telemetry.")
        return {**payload, "phases": {**payload["phases"], **verifier["phases"]}}

    def check(self) -> str | None:
        payload = self._phase_record()
        now = self.clock.capture()
        if now.continuous_sec < self.latest:
            raise ValueError("Host continuous clock regressed.")
        self.latest = now.continuous_sec
        self.report["last_sample"] = asdict(now)
        if payload is None:
            return "environment_start_timeout" if self.latest >= self.initial_deadline else None
        phases = payload.get("phases")
        if not isinstance(phases, dict) or not phases:
            raise ValueError("Budget phases are missing.")
        if not self.first_starts.keys() <= phases.keys():
            raise ValueError("Previously observed budget phases disappeared.")
        for phase, record in phases.items():
            if phase not in self.limits or not isinstance(record, dict):
                raise ValueError("Unsupported budget phase.")
            started = record.get("started", {}).get("continuous_sec")
            if (
                isinstance(started, bool) or not isinstance(started, (int, float))
                or not math.isfinite(started) or not self.started.continuous_sec <= started <= self.latest
            ):
                raise ValueError("Invalid phase start time.")
            self.first_starts.setdefault(phase, started)
            if self.first_starts[phase] != started:
                raise ValueError("Budget phase was restarted.")
            if record.get("status") != "running":
                finished = record.get("finished", {}).get("continuous_sec")
                if (
                    isinstance(finished, bool) or not isinstance(finished, (int, float))
                    or not math.isfinite(finished) or not started <= finished <= self.latest
                ):
                    raise ValueError("Invalid phase finish time.")
                self.first_finishes.setdefault(phase, finished)
                if self.first_finishes[phase] != finished:
                    raise ValueError("Budget finish time was changed.")
            elif phase in self.first_finishes:
                raise ValueError("A finished budget phase was reopened.")
        if "agent" in self.first_starts:
            self.report["possible_partial_execution"] = True
        if "verification" in phases:
            for earlier in ("setup", "agent", "cleanup"):
                if earlier in self.first_starts and earlier not in self.first_finishes:
                    raise ValueError(f"Verification overlapped {earlier}.")
            if "cleanup" in self.first_finishes:
                if self.first_starts["verification"] < self.first_finishes["cleanup"]:
                    raise ValueError("Verification preceded cleanup.")
                if self.latest >= self.first_finishes["cleanup"] + self.post_agent_sec:
                    return "post_agent_timeout"
        for phase in ("verification", "cleanup", "agent", "setup"):
            record = phases.get(phase)
            if not record:
                continue
            self.report["phase"] = phase
            started = self.first_starts[phase]
            if record.get("status") == "running":
                # A stuck agent gets the same bounded cleanup allowance as Harbor.
                allowance = CLEANUP_ALLOWANCE_SEC if phase in {"setup", "agent"} else 0.0
                cutoff = started + self.limits[phase] + allowance
                return f"{phase}_outer_timeout" if self.latest >= cutoff else None
            if record.get("status") not in {"completed", "timed_out", "cancelled", "failed"}:
                raise ValueError("Invalid terminal budget status.")
            finished = record.get("finished", {}).get("continuous_sec")
            if (
                isinstance(finished, bool) or not isinstance(finished, (int, float))
                or not math.isfinite(finished) or not started <= finished <= self.latest
            ):
                raise ValueError("Invalid phase finish time.")
            if phase == "cleanup":
                self.report["phase"] = "verification_and_finalize"
                return "post_agent_timeout" if self.latest >= finished + self.post_agent_sec else None
            if phase == "verification":
                self.report["phase"] = "finalize"
                return "finalize_timeout" if self.latest >= finished + TRANSITION_ALLOWANCE_SEC else None
            # Between phases only; never allow repeated edits to extend the cutoff.
            return "phase_transition_timeout" if self.latest >= finished + TRANSITION_ALLOWANCE_SEC else None
        raise ValueError("No recognized budget phase.")

    def start(self, process, terminate) -> None:
        self._record()

        def supervise():
            while not self._done.wait(POLL_SEC):
                # A child may retain stdout after Harbor's leader has exited.
                # Supervise until the reader explicitly closes the trial.
                try:
                    reason = self.check()
                except Exception as error:
                    reason = "invalid_phase_evidence"
                    self.report["error_type"] = type(error).__name__
                if reason:
                    self.report.update(status="terminated", reason=reason,
                                       possible_partial_execution=True)
                    self._record()
                    self.report["cleanup_errors"] = terminate(process)
                    self._record()
                    return

        self._thread = threading.Thread(target=supervise, name="forgeflow-run-watchdog", daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        self._done.set()
        if self._thread is not None:
            self._thread.join(timeout=CLEANUP_ALLOWANCE_SEC + 11)
        if self.report["status"] == "running":
            self.report["status"] = "finished"
        self.report["finished"] = asdict(self.clock.capture())
        self._record()
        return dict(self.report)

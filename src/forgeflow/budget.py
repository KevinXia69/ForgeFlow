"""Suspend-aware host deadlines and bounded, credential-free phase telemetry.

UTC and ordinary monotonic time are observations, never the budget authority.
An external supervisor is still required if this Python process cannot schedule
or an environment implementation suppresses cancellation indefinitely.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import math
import os
import sys
import time
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

BUDGET_MECHANISM_VERSION = "host-continuous-v1"
CLEANUP_ALLOWANCE_SEC = 30.0
SETUP_BUDGET_SEC = 1800.0
POLL_INTERVAL_SEC = 0.1
T = TypeVar("T")


class BudgetClockUnavailable(RuntimeError):
    """No reviewed suspend-aware clock is available; do not start execution."""


class BudgetExceeded(TimeoutError):
    """The continuous execution budget has expired; writes may be partial."""


class BudgetCleanupError(RuntimeError):
    """Cleanup could not confirm quiescence; do not report a clean success."""


def positive_seconds(value: float, *, name: str = "budget") -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} must be a finite positive number of seconds.")
    value = float(value)
    if not math.isfinite(value) or value <= 0:
        raise ValueError(f"{name} must be a finite positive number of seconds.")
    return value


@dataclass(frozen=True)
class ClockSample:
    utc: str
    utc_sec: float
    continuous_sec: float
    monotonic_sec: float
    process_cpu_sec: float


@dataclass(frozen=True)
class BudgetClock:
    source: str
    continuous: Callable[[], float]
    monotonic: Callable[[], float] = time.monotonic
    utc: Callable[[], float] = time.time
    process_cpu: Callable[[], float] = time.process_time

    def capture(self) -> ClockSample:
        continuous = self.continuous()
        monotonic = self.monotonic()
        utc = self.utc()
        cpu = self.process_cpu()
        if not all(math.isfinite(v) for v in (continuous, monotonic, utc, cpu)):
            raise BudgetClockUnavailable("Invalid clock sample; execution is blocked.")
        return ClockSample(
            datetime.fromtimestamp(utc, UTC).isoformat(), utc, continuous, monotonic, cpu,
        )


def system_clock() -> BudgetClock:
    """Return a host clock that includes suspend, or fail closed.

    Linux CLOCK_BOOTTIME includes suspended time. Darwin mach_continuous_time
    includes sleep, unlike relying on an unspecified asyncio/monotonic clock.
    This says nothing about how a VM's virtual clocks follow host suspension.
    """
    if sys.platform.startswith("linux") and hasattr(time, "CLOCK_BOOTTIME"):
        return BudgetClock("linux-clock-boottime", lambda: time.clock_gettime(time.CLOCK_BOOTTIME))
    if sys.platform == "darwin":
        class Timebase(ctypes.Structure):
            _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]

        library = ctypes.CDLL(None)
        try:
            continuous = library.mach_continuous_time
            timebase = library.mach_timebase_info
        except AttributeError as exc:
            raise BudgetClockUnavailable("Darwin continuous clock is unavailable.") from exc
        continuous.argtypes = []
        continuous.restype = ctypes.c_uint64
        timebase.argtypes = [ctypes.POINTER(Timebase)]
        timebase.restype = ctypes.c_int
        info = Timebase()
        if timebase(ctypes.byref(info)) != 0 or not info.denom:
            raise BudgetClockUnavailable("Darwin continuous clock timebase is unavailable.")
        scale = info.numer / info.denom / 1_000_000_000
        return BudgetClock("darwin-mach-continuous-time", lambda: continuous() * scale)
    raise BudgetClockUnavailable("A reviewed suspend-aware host clock is required.")


class Deadline:
    def __init__(self, clock: BudgetClock, seconds: float):
        self.clock = clock
        self.limit_sec = positive_seconds(seconds)
        self.started = clock.capture()
        self.deadline_continuous_sec = self.started.continuous_sec + self.limit_sec
        self._latest = self.started.continuous_sec

    def remaining(self) -> float:
        observed = self.clock.continuous()
        if not math.isfinite(observed) or observed < self._latest:
            raise BudgetClockUnavailable("Continuous clock regressed; execution is blocked.")
        self._latest = observed
        return max(0.0, self.deadline_continuous_sec - observed)

    def check(self) -> None:
        if not self.remaining():
            raise BudgetExceeded("Continuous budget expired; execution outcome may be unknown.")


def _consume_task_result(task: asyncio.Future) -> None:
    if not task.cancelled():
        task.exception()


async def bounded_await(
    operation: Callable[[], Awaitable[T]],
    deadline: Deadline,
    *,
    on_poll: Callable[[], None] | None = None,
    poll_interval: float = POLL_INTERVAL_SEC,
) -> T:
    """Do not dispatch after expiry or wait indefinitely for cancellation.

    Cancelling a transport request is not evidence that a remote write stopped.
    The caller must separately stop the executor and preserve its journal.
    """
    positive_seconds(poll_interval, name="poll interval")
    deadline.check()
    task = asyncio.ensure_future(operation())
    try:
        while True:
            deadline.check()
            if on_poll is not None:
                on_poll()
            if task.done():
                return task.result()
            await asyncio.wait({task}, timeout=min(poll_interval, deadline.remaining()))
    finally:
        if not task.done():
            task.cancel()
            task.add_done_callback(_consume_task_result)


class BudgetRecorder:
    """An atomic fixed-schema record; never accepts exception text or prompts."""

    def __init__(self, path: Path, clock: BudgetClock):
        self.path = path
        self.clock = clock
        self.data: dict = {
            "schema": 1,
            "mechanism": BUDGET_MECHANISM_VERSION,
            "clock": {"source": clock.source, "includes_suspend": True,
                      "process_cpu_scope": "host-harbor-process"},
            "phases": {},
            "last_sample": None,
            "dispatch_started": False,
            "possible_partial_execution": False,
            "cleanup_quiescence": "unknown",
            "container_samples": [],
        }

    def write(self) -> None:
        self.data["last_sample"] = asdict(self.clock.capture())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(self.data, output, sort_keys=True)
            output.write("\n")
        os.replace(temporary, self.path)

    def start(self, phase: str, seconds: float) -> Deadline:
        if phase not in {"setup", "agent", "cleanup", "verification"}:
            raise ValueError("Unknown budget phase.")
        if phase in self.data["phases"]:
            raise ValueError("A budget phase cannot be restarted.")
        deadline = Deadline(self.clock, seconds)
        self.data["phases"][phase] = {
            "limit_sec": deadline.limit_sec,
            "deadline_continuous_sec": deadline.deadline_continuous_sec,
            "started": asdict(deadline.started), "finished": None, "status": "running",
        }
        self.write()
        return deadline

    def finish(self, phase: str, status: str) -> None:
        if status not in {"completed", "timed_out", "cancelled", "failed"}:
            raise ValueError("Unknown phase outcome.")
        record = self.data["phases"][phase]
        finished = asdict(self.clock.capture())
        started = record["started"]
        elapsed = {
            name: finished[name] - started[name]
            for name in ("utc_sec", "continuous_sec", "monotonic_sec", "process_cpu_sec")
        }
        record.update(
            finished=finished, status=status, elapsed=elapsed,
            utc_minus_continuous_sec=elapsed["utc_sec"] - elapsed["continuous_sec"],
            continuous_minus_monotonic_sec=elapsed["continuous_sec"] - elapsed["monotonic_sec"],
        )
        self.write()


# Root installs this fixed extension. It reads only a non-secret, root-owned
# deadline. No model-supplied timestamp can extend either clock's cutoff.
PI_BUDGET_EXTENSION = r'''import { readFileSync, existsSync } from "node:fs";

export function assertBudget(read = readFileSync, exists = existsSync, utc = Date.now) {
  const fail = () => { throw new Error("ForgeFlow execution budget unavailable or expired."); };
  try {
    if (exists("/opt/forgeflow/budget.closed")) fail();
    const b = JSON.parse(read("/opt/forgeflow/budget.json", "utf8"));
    const uptime = Number(read("/proc/uptime", "utf8").trim().split(/\s+/)[0]);
    const now = utc() / 1000;
    if (b.mechanism !== "host-continuous-v1" ||
        !Number.isFinite(uptime) || !Number.isFinite(now) ||
        !Number.isFinite(b.started_boottime_sec) ||
        !Number.isFinite(b.deadline_boottime_sec) ||
        !Number.isFinite(b.deadline_utc_sec) ||
        b.deadline_boottime_sec <= b.started_boottime_sec ||
        uptime < b.started_boottime_sec || uptime >= b.deadline_boottime_sec ||
        now >= b.deadline_utc_sec) fail();
  } catch { fail(); }
}

export default function budgetGuard(pi) {
  const stopIfExpired = () => {
    try { assertBudget(); } catch { process.exit(124); }
  };
  // Extension errors can be caught by Pi; process exit is the fail-closed
  // boundary for startup and a new provider request. Never inspect its payload.
  stopIfExpired();
  pi.on("before_provider_request", () => { stopIfExpired(); });
  // Blocks every tool, including shell and native recovery, before dispatch.
  pi.on("tool_call", async () => {
    try { assertBudget(); }
    catch { return { block: true, terminate: true,
      reason: "ForgeFlow execution budget unavailable or expired." }; }
  });
  // The pinned Pi catches before_agent_start exceptions and continues, so that
  // event is deliberately not used as a hard stop. This also bounds a model
  // request that has not produced any tools. Root cleanup owns in-flight writes.
  const timer = setInterval(stopIfExpired, 100);
  timer.unref();
}
'''

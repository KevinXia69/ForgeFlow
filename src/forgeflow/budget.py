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
TOKEN_BUDGET_MECHANISM = "reported-usage-gate-v1"
DEFAULT_PREPARATION_TOKEN_BUDGET = 2_000_000
MIN_NEXT_CALL_TOKENS = 1024
BUSINESS_AUDIT_TABLES = (
    "purchase_order", "purchase_order_line", "mrp_production", "mrp_workorder",
    "sale_order", "sale_order_line", "stock_move", "stock_picking", "stock_quant",
    "mrp_bom", "mrp_bom_line", "mrp_workcenter", "product_supplierinfo",
    "product_product", "product_template", "res_partner",
)
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


def resolve_total_token_budget(value: int | None, *, agent: str, recovery_mode: str) -> int | None:
    """Reported main-loop usage gate; an in-flight response can exceed the cap."""
    if value is not None:
        if agent != "pi":
            raise ValueError("max_total_tokens is supported only for the Pi agent.")
        if type(value) is not int or not 0 < value <= 2**53 - 1:
            raise ValueError("max_total_tokens must be a positive safe integer.")
    if value is None and recovery_mode in ("prepare-only", "investigation"):
        return DEFAULT_PREPARATION_TOKEN_BUDGET
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
PI_BUDGET_EXTENSION = r'''import { readFileSync, existsSync, openSync, writeFileSync, closeSync, constants } from "node:fs";

// Pinned Pi AssistantMessage.usage.totalTokens includes cache and reasoning
// accounting already. Only message_end is consumed; turn_end is the same answer.
export function createTokenBudget(policy, report = () => {}) {
  if (policy === undefined || policy === null) return null;
  if (policy.mechanism !== "reported-usage-gate-v1"
      || !Number.isSafeInteger(policy.max_total_tokens) || policy.max_total_tokens <= 0
      || !Number.isSafeInteger(policy.min_next_call_tokens) || policy.min_next_call_tokens <= 0) {
    throw Error("token_budget_unavailable");
  }
  const state = {schema_version:1, mechanism:policy.mechanism,
    max_total_tokens:policy.max_total_tokens, min_next_call_tokens:policy.min_next_call_tokens,
    reported_total_tokens:0, completed_responses:0, provider_requests:0,
    in_flight:false, partial_usage_unknown:false, stop_reason:null,
    scope:"main_pi_provider_requests", in_flight_can_exceed_limit:true,
    output_cap_policy:"remaining_allowance_after_input_reservation_v1"};
  const emit = () => report({...state});
  const stop = reason => { state.stop_reason ||= reason; emit(); throw Error(state.stop_reason); };
  const check = () => {
    if (state.stop_reason) stop(state.stop_reason);
    if (state.partial_usage_unknown) stop("token_usage_unknown");
    if (state.reported_total_tokens >= state.max_total_tokens) stop("token_budget_exhausted");
  };
  emit();
  return {
    beforeProvider(payload) {
      check();
      // A retry/auxiliary request without a completed accounted response cannot
      // assume the previous request was free. Stop rather than reset its usage.
      if (state.in_flight) { state.partial_usage_unknown=true; stop("token_usage_unknown"); }
      if (state.reported_total_tokens + state.min_next_call_tokens > state.max_total_tokens) {
        stop("token_budget_exhausted");
      }
      let capped = payload;
      if (payload !== undefined) {
        const allowance = state.max_total_tokens - state.reported_total_tokens;
        capped = capPayloadToRemaining(payload, allowance);
        if (!capped) stop("token_budget_exhausted");
        state.last_request_max_output_tokens = capped.max_tokens ?? capped.max_completion_tokens;
        state.last_input_reservation_tokens = inputReservation(payload);
      }
      state.provider_requests++; state.in_flight=true; emit();
      return capped;
    },
    messageEnd(event) {
      const message = event?.message;
      if (message?.role !== "assistant") return;
      if (!state.in_flight) { state.partial_usage_unknown=true; stop("token_usage_unknown"); }
      state.in_flight=false; state.completed_responses++;
      const total=message.usage?.totalTokens;
      // This Pi provider initializes absent usage to all zeros, so zero cannot
      // establish a free completed call. Error/aborted responses can be partial.
      if (!Number.isSafeInteger(total) || total <= 0
          || !Number.isSafeInteger(state.reported_total_tokens + total)) {
        state.partial_usage_unknown=true;
      } else { state.reported_total_tokens += total; }
      if (["error","aborted","pending","deferred"].includes(message.stopReason)
          || !["stop","length","toolUse"].includes(message.stopReason)) state.partial_usage_unknown=true;
      if (state.partial_usage_unknown) state.stop_reason="token_usage_unknown";
      else if (state.reported_total_tokens >= state.max_total_tokens) state.stop_reason="token_budget_exhausted";
      emit();
    },
    beforeTool() { check(); if (state.in_flight) { state.partial_usage_unknown=true; stop("token_usage_unknown"); } },
    interrupted() { if (state.in_flight) { state.partial_usage_unknown=true; state.stop_reason ||= "token_usage_unknown"; } emit(); },
    snapshot() { return {...state}; },
  };
}

// This surface sends text-only messages/tools. UTF-8 bytes plus wrapper reserve
// is deliberately conservative; it is not claimed to be exact billed usage.
// Final provider receipts remain authoritative, including any overrun/unknown.
export function inputReservation(payload) {
  return Buffer.byteLength(JSON.stringify(payload), "utf8") + 4096;
}

export function capPayloadToRemaining(payload, allowance) {
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) return null;
  const remaining = allowance - inputReservation(payload);
  if (!Number.isSafeInteger(remaining) || remaining <= 0) return null;
  const result = { ...payload };
  const keys = ["max_tokens", "max_completion_tokens"].filter(key => key in result);
  if (keys.length === 0) keys.push("max_tokens");
  for (const key of keys) {
    const configured = result[key];
    if (configured !== undefined && (!Number.isSafeInteger(configured) || configured <= 0)) return null;
    result[key] = Math.min(configured ?? remaining, remaining);
  }
  return result;
}

function tokenReport(value) {
  const fd = openSync("/home/forgeflow/logs/token-budget.json",
    constants.O_WRONLY | constants.O_CREAT | constants.O_TRUNC | constants.O_NOFOLLOW, 0o600);
  try { writeFileSync(fd, JSON.stringify(value) + "\n"); } finally { closeSync(fd); }
}

export function requestOutputPolicy(payload) {
  const positive = value => Number.isSafeInteger(value) && value > 0 ? value : null;
  return {
    schema_version: 1, policy: "context_aware_output_ceiling", policy_version: "pi-context-clamp-v1",
    observation: "before_provider_request", provider_receipt_confirmed: false,
    max_tokens: positive(payload?.max_tokens),
    max_completion_tokens: positive(payload?.max_completion_tokens),
    message_count: Array.isArray(payload?.messages) ? payload.messages.length : null,
  };
}

function outputPolicyReport(payload) {
  const fd = openSync("/home/forgeflow/logs/provider-request-policy.jsonl",
    constants.O_WRONLY | constants.O_CREAT | constants.O_APPEND | constants.O_NOFOLLOW, 0o600);
  try { writeFileSync(fd, JSON.stringify(requestOutputPolicy(payload)) + "\n"); }
  finally { closeSync(fd); }
}

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
  let tokens;
  try { tokens=createTokenBudget(JSON.parse(readFileSync("/opt/forgeflow/budget.json", "utf8")).token_budget, tokenReport); }
  catch { process.exit(125); }
  globalThis.__forgeflowTokenSnapshot = () => tokens?.snapshot() ?? null;
  let finalDispatches = 0;
  globalThis.__forgeflowProviderRequestBudget = payload => {
    stopIfExpired();
    if (!tokens) return payload;
    const value = tokens.snapshot();
    // Compaction/auxiliary requests without a counted main-loop permit cannot
    // silently escape the allowance or fabricate a completed usage receipt.
    if (!value.in_flight || finalDispatches >= value.provider_requests) process.exit(125);
    const capped = capPayloadToRemaining(payload, value.max_total_tokens - value.reported_total_tokens);
    if (!capped) process.exit(125);
    finalDispatches++;
    return capped;
  };
  const stopIfExpired = () => {
    try { assertBudget(); } catch { tokens?.interrupted(); process.exit(124); }
  };
  // Extension errors can be caught by Pi; process exit is the fail-closed
  // boundary for startup and a new provider request. Audit only numeric output
  // ceilings and message count, never prompt, tool, credential or response text.
  stopIfExpired();
  pi.on("before_provider_request", event => {
    stopIfExpired();
    // No call accounting or provider request after a trusted terminal. The
    // normal stop occurs after the complete tool receipt at turn_end; this is
    // the fail-closed fallback, including a mixed tool batch or extra Pi turn.
    if (globalThis.__forgeflowInvestigationTerminal?.beforeProvider(false) === false) process.exit(0);
    try {
      const payload = tokens ? tokens.beforeProvider(event.payload) : event.payload;
      globalThis.__forgeflowInvestigationTerminal?.beforeProvider(true);
      outputPolicyReport(payload);
      return payload;
    }
    catch { process.exit(125); }
  });
  // Blocks every tool, including shell and native recovery, before dispatch.
  pi.on("tool_call", async () => {
    try { assertBudget(); tokens?.beforeTool(); }
    catch { return { block: true, terminate: true,
      reason: tokens?.snapshot().stop_reason || "ForgeFlow execution budget unavailable or expired." }; }
  });
  if (tokens) {
    pi.on("message_end", event => {
      try { tokens.messageEnd(event); } catch { process.exit(125); }
    });
    process.on("exit", () => { try { tokens.interrupted(); } catch {} });
  }
  // The pinned Pi catches before_agent_start exceptions and continues, so that
  // event is deliberately not used as a hard stop. This also bounds a model
  // request that has not produced any tools. Root cleanup owns in-flight writes.
  const timer = setInterval(stopIfExpired, 100);
  timer.unref();
}
'''

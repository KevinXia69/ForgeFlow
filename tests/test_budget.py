from __future__ import annotations

import asyncio
import json
import math
import shutil
import subprocess
import sys
import time

import pytest

from forgeflow.budget import (
    PI_BUDGET_EXTENSION,
    BudgetClock,
    BudgetClockUnavailable,
    BudgetExceeded,
    BudgetRecorder,
    Deadline,
    bounded_await,
    positive_seconds,
    system_clock,
)


class SimulatedClocks:
    def __init__(self):
        self.continuous = 100.0
        self.monotonic = 100.0
        self.utc = 1_800_000_000.0
        self.clock = BudgetClock(
            "synthetic-continuous", lambda: self.continuous,
            lambda: self.monotonic, lambda: self.utc, lambda: 0.01,
        )


@pytest.mark.parametrize("bad", [True, False, 0, -1, math.inf, math.nan, "600", None])
def test_bad_budgets_fail_before_dispatch(bad):
    with pytest.raises(ValueError):
        positive_seconds(bad)


@pytest.mark.parametrize("utc_jump", [-7200, 7200])
def test_wall_clock_jump_does_not_change_continuous_budget(utc_jump):
    clocks = SimulatedClocks()
    deadline = Deadline(clocks.clock, 600)
    clocks.continuous += 25
    clocks.utc += utc_jump
    assert deadline.remaining() == 575
    clocks.continuous += 575
    with pytest.raises(BudgetExceeded):
        deadline.check()


def test_suspend_uses_continuous_clock_when_monotonic_is_frozen():
    clocks = SimulatedClocks()
    deadline = Deadline(clocks.clock, 600)
    clocks.continuous += 650
    clocks.utc += 650
    assert clocks.monotonic == 100
    with pytest.raises(BudgetExceeded):
        deadline.check()


def test_continuous_clock_regression_fails_closed():
    clocks = SimulatedClocks()
    deadline = Deadline(clocks.clock, 600)
    clocks.continuous -= 1
    with pytest.raises(BudgetClockUnavailable):
        deadline.check()


def test_host_clock_is_reviewed_and_sample_is_finite():
    clock = system_clock()
    sample = clock.capture()
    assert clock.source in {"linux-clock-boottime", "darwin-mach-continuous-time"}
    assert math.isfinite(sample.continuous_sec)
    assert sample.utc.endswith("+00:00")


def test_unsupported_platform_cannot_silently_fall_back(monkeypatch):
    monkeypatch.setattr("forgeflow.budget.sys.platform", "unsupported")
    with pytest.raises(BudgetClockUnavailable):
        system_clock()


def test_expired_budget_does_not_create_operation():
    clocks = SimulatedClocks()
    deadline = Deadline(clocks.clock, 1)
    clocks.continuous += 2
    dispatched = []

    async def operation():
        dispatched.append(True)

    with pytest.raises(BudgetExceeded):
        asyncio.run(bounded_await(operation, deadline))
    assert dispatched == []


def test_simulated_sleep_cancels_running_operation_without_new_dispatch():
    clocks = SimulatedClocks()
    events = []

    async def scenario():
        async def hanging():
            events.append("started")
            clocks.continuous += 601
            clocks.utc += 601
            try:
                await asyncio.Event().wait()
            finally:
                events.append("cancelled")

        with pytest.raises(BudgetExceeded):
            await bounded_await(hanging, Deadline(clocks.clock, 600), poll_interval=0.001)
        await asyncio.sleep(0)

    asyncio.run(scenario())
    assert events == ["started", "cancelled"]


def test_hung_real_subprocess_gets_bounded_kill_without_claiming_cancellation_stopped_it():
    async def scenario():
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-c", "import time; time.sleep(60)",
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
        )
        started = time.monotonic()
        try:
            with pytest.raises(BudgetExceeded):
                await bounded_await(process.wait, Deadline(system_clock(), 0.03), poll_interval=0.005)
            # Transport cancellation did not establish subprocess termination.
            assert process.returncode is None
            process.kill()
            await bounded_await(process.wait, Deadline(system_clock(), 1))
            assert process.returncode != 0
            assert time.monotonic() - started < 2
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    asyncio.run(scenario())


def test_phase_records_distinguish_agent_end_and_cleanup_and_keep_unknown(tmp_path):
    clocks = SimulatedClocks()
    record = BudgetRecorder(tmp_path / "budget.json", clocks.clock)
    record.start("agent", 600)
    record.data["dispatch_started"] = True
    clocks.continuous += 601
    record.data["possible_partial_execution"] = True
    record.finish("agent", "timed_out")
    record.start("cleanup", 30)
    payload = json.loads(record.path.read_text())
    assert payload["phases"]["agent"]["finished"] is not None
    assert payload["phases"]["cleanup"]["finished"] is None
    assert payload["phases"]["agent"]["limit_sec"] == 600
    assert payload["phases"]["cleanup"]["deadline_continuous_sec"] == 731
    assert payload["possible_partial_execution"] is True
    assert payload["cleanup_quiescence"] == "unknown"
    assert not record.path.with_suffix(".tmp").exists()


def test_container_gate_blocks_expiry_even_if_host_stop_flag_has_not_arrived(tmp_path):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the real JS boundary test")
    module = tmp_path / "budget_guard.mjs"
    uptime_path = tmp_path / "uptime"
    uptime_path.write_text("101.00 99.00")
    (tmp_path / "budget.json").write_text(json.dumps({
        "mechanism": "host-continuous-v1", "started_boottime_sec": 100,
        "deadline_boottime_sec": 700, "deadline_utc_sec": time.time() + 600,
    }))
    module.write_text(PI_BUDGET_EXTENSION.replace(
        "/opt/forgeflow", str(tmp_path),
    ).replace("/proc/uptime", str(uptime_path)))
    driver = tmp_path / "test.mjs"
    driver.write_text(r'''
import assert from 'node:assert/strict';
import {writeFileSync} from 'node:fs';
import budgetGuard, { assertBudget } from './budget_guard.mjs';
const base = {mechanism:'host-continuous-v1', started_boottime_sec:100,
  deadline_boottime_sec:700, deadline_utc_sec:1800000600};
let b = {...base}, uptime = '101.00 99.00', now = 1800000001000;
const read = p => p === '/proc/uptime' ? uptime : JSON.stringify(b);
const check = (closed=false) => assertBudget(read, () => closed, () => now);
check();
// Guest suspend: uptime advances even though its ordinary monotonic is frozen.
uptime='701.00 99.00'; now=1800000001000; assert.throws(() => check());
// UTC can stop early, but rolling it back cannot extend the boottime budget.
uptime='101.00 99.00'; now=1800000700000; assert.throws(() => check());
uptime='701.00 99.00'; now=1799990000000; assert.throws(() => check());
uptime='101.00 99.00'; now=1800000001000; assert.throws(() => check(true));
for (const bad of [NaN, Infinity, '700', null]) {
  b={...base,deadline_boottime_sec:bad}; assert.throws(() => check());
}
b={...base}; uptime='not-a-clock'; assert.throws(() => check());
assert.throws(() => assertBudget(() => {throw new Error('secret-canary')}, () => false),
  e => !e.message.includes('secret-canary'));
const handlers = {};
budgetGuard({on: (name, callback) => { handlers[name]=callback; }});
assert.deepEqual(Object.keys(handlers), ['before_provider_request','tool_call']);
assert.equal(await handlers.tool_call({toolName:'read'}), undefined);
// The model has no way to extend the root-owned deadline through tool input.
writeFileSync(new URL('./uptime', import.meta.url), '701.00 99.00');
const refused = await handlers.tool_call({toolName:'execute_recovery'});
assert.equal(refused.block, true);
assert.equal(refused.terminate, true);
console.log('PASS');
'''.replace("/proc/uptime", str(uptime_path)))
    result = subprocess.run([node, str(driver)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"


@pytest.mark.parametrize("stage", ["startup", "provider", "timer"])
def test_expired_container_budget_exits_even_without_tools(tmp_path, stage):
    node = shutil.which("node")
    if not node:
        pytest.skip("Node is needed for the real JS process boundary test")
    uptime = tmp_path / "uptime"
    uptime.write_text("701 1" if stage == "startup" else "101 1")
    (tmp_path / "budget.json").write_text(json.dumps({
        "mechanism": "host-continuous-v1", "started_boottime_sec": 100,
        "deadline_boottime_sec": 700, "deadline_utc_sec": time.time() + 600,
    }))
    (tmp_path / "guard.mjs").write_text(PI_BUDGET_EXTENSION.replace(
        "/opt/forgeflow", str(tmp_path),
    ).replace("/proc/uptime", str(uptime)))
    driver = tmp_path / "driver.mjs"
    driver.write_text("""
import guard from './guard.mjs';
import {writeFileSync} from 'node:fs';
const handlers = {};
guard({on:(name, callback)=>{handlers[name]=callback}});
writeFileSync(new URL('./uptime', import.meta.url),'701 1');
if (process.argv[2] === 'provider') {
  handlers.before_provider_request(new Proxy({}, {get:()=>{throw new Error('payload was read')}}));
  writeFileSync(new URL('./new-dispatch', import.meta.url),'must not happen');
}
setInterval(()=>{}, 1000);
""")
    result = subprocess.run([node, str(driver), stage], capture_output=True, timeout=2)
    assert result.returncode == 124
    assert not (tmp_path / "new-dispatch").exists()
    assert result.stdout == b"" and result.stderr == b""

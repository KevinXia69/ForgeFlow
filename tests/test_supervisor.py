"""Process supervision: a crashed loop is restarted, then reported unhealthy."""
from __future__ import annotations

import threading

from forgeflow.superagent import Supervisor


class Loop:
    def __init__(self, failures: int, *, block: bool = False) -> None:
        self.failures = failures
        self.block = block
        self.calls = 0

    def run(self, stop_event: threading.Event) -> int:
        self.calls += 1
        if self.calls <= self.failures:
            raise RuntimeError("transient failure")
        if self.block:
            stop_event.wait()
            return 0
        stop_event.set()
        return 0


def test_a_crashed_loop_is_restarted() -> None:
    supervisor = Supervisor(max_restarts=3, backoff_seconds=0.01)
    loop = Loop(failures=2)
    supervisor.register("source", loop)
    supervisor.start()
    for _ in range(200):
        state = supervisor.status()["source"]
        if state["finished"]:
            break
        threading.Event().wait(0.01)
    supervisor.stop()
    state = supervisor.status()["source"]
    assert loop.calls >= 3
    assert state["restarts"] == 2
    assert state["error"].startswith("RuntimeError")
    assert state["healthy"] is True


def test_restart_budget_is_bounded_and_reported() -> None:
    supervisor = Supervisor(max_restarts=1, backoff_seconds=0.01)
    loop = Loop(failures=10)
    supervisor.register("dispatcher", loop)
    supervisor.start()
    for _ in range(200):
        if supervisor.status()["dispatcher"]["finished"]:
            break
        threading.Event().wait(0.01)
    supervisor.stop()
    state = supervisor.status()["dispatcher"]
    assert state["finished"] is True
    assert state["restarts"] == 1
    assert state["healthy"] is False
    assert supervisor.unhealthy() == ["dispatcher"]


def test_stop_is_not_blocked_by_a_backoff() -> None:
    supervisor = Supervisor(max_restarts=5, backoff_seconds=5.0)
    supervisor.register("slow", Loop(failures=5))
    supervisor.start()
    threading.Event().wait(0.05)
    supervisor.stop()
    assert supervisor.status()["slow"]["finished"] is True


def test_a_finished_loop_is_not_restarted() -> None:
    supervisor = Supervisor(max_restarts=3, backoff_seconds=0.01)
    loop = Loop(failures=0)
    supervisor.register("watchdog", loop)
    supervisor.start()
    for _ in range(200):
        if supervisor.status()["watchdog"]["finished"]:
            break
        threading.Event().wait(0.01)
    supervisor.stop()
    assert loop.calls == 1
    assert supervisor.status()["watchdog"]["restarts"] == 0

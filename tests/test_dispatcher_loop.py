from __future__ import annotations

import threading

from forgeflow.superagent import DispatcherLoop


class FakeDispatcher:
    def __init__(self):
        self.calls = 0

    def tick(self, worker_id, tenant_id=None):
        self.calls += 1
        return None


def test_dispatcher_loop_stops_without_side_effects() -> None:
    fake = FakeDispatcher()
    loop = DispatcherLoop(fake, "worker-a", interval_seconds=0.01)
    stop = threading.Event()
    threading.Timer(0.03, stop.set).start()
    assert loop.run(stop) == 0
    assert fake.calls >= 1

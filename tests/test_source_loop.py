from __future__ import annotations

import threading

from forgeflow.superagent import SourceEvent, SourceLoop


class FakeSource:
    def __init__(self):
        self.scans = 0

    def scan(self):
        self.scans += 1
        return [SourceEvent("test", f"event-{self.scans}", "a" * 64, "PO-1", "investigate")]

    def admit(self, engine, ctx, events):
        return [event.external_event_id for event in events]


class FakeEngine:
    pass


class Ctx:
    pass


def test_source_loop_is_stop_aware() -> None:
    source = FakeSource()
    loop = SourceLoop(source, interval_seconds=0.01)
    stop = threading.Event()
    threading.Timer(0.03, stop.set).start()
    assert loop.run(FakeEngine(), Ctx(), stop) >= 1
    assert source.scans >= 1

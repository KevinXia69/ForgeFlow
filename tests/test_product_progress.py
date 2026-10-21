from __future__ import annotations

from forgeflow.product.progress import ProgressHub


def test_progress_hub_is_ordered_and_resettable():
    hub = ProgressHub()
    hub.reset("case")
    hub.emit("case", "apply", "active")
    hub.emit("case", "verify", "done", "passed")
    events = hub.snapshot("case")
    assert [event["phase"] for event in events] == ["apply", "verify"]
    assert events[1]["detail"] == "passed"
    hub.reset("case")
    assert hub.snapshot("case") == []


def test_progress_hub_isolates_cases():
    hub = ProgressHub()
    hub.emit("a", "apply", "active")
    hub.emit("b", "verify", "failed")
    assert [event["phase"] for event in hub.snapshot("a")] == ["apply"]
    assert [event["status"] for event in hub.snapshot("b")] == ["failed"]

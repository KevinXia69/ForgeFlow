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


def test_progress_sequence_survives_reset_and_never_reuses_old_offsets():
    hub = ProgressHub()
    hub.emit("case", "read", "active")
    hub.emit("case", "plan", "active")
    hub.emit("case", "policy", "done")
    before = hub.snapshot("case")
    assert [event["seq"] for event in before] == [1, 2, 3]
    hub.reset("case")
    hub.emit("case", "approval", "done")
    hub.emit("case", "apply", "active")
    hub.emit("case", "execution", "done")
    after = hub.snapshot("case")
    assert [event["seq"] for event in after] == [4, 5, 6]
    assert {event["phase"] for event in after} == {"approval", "apply", "execution"}
    advanced = [event for event in after if event["seq"] > before[-1]["seq"]]
    assert len(advanced) == 3

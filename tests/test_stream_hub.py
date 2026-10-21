from __future__ import annotations

import json

from forgeflow.api.stream_hub import MAX_EVENTS, AssistantStreamHub


def _frames(text: str) -> list[dict]:
    events = []
    for block in text.strip().split("\n\n"):
        data = [line[5:].strip() for line in block.splitlines() if line.startswith("data:")]
        if data:
            events.append(json.loads("".join(data)))
    return events


def test_reconnect_replays_only_missed_events():
    hub = AssistantStreamHub()
    started = __import__("threading").Event()
    release = __import__("threading").Event()

    def producer():
        started.set()
        yield {"type": "trace", "activity": [{"kind": "think", "label": "a", "status": "done"}]}
        release.wait(2)
        yield {"type": "final", "answer": "ok"}

    job = hub.start("k", producer)
    started.wait(2)
    first = []
    for frame in job.subscribe(None):
        first.append(frame)
        if len(first) == 1:
            break
    assert _frames("".join(first))[0]["type"] == "trace"
    release.set()
    job._thread.join(2)
    resumed = _frames("".join(job.subscribe(1)))
    assert resumed[0]["type"] == "final"
    assert resumed[0]["answer"] == "ok"


def test_new_start_reuses_an_active_job_and_forgets_finished_jobs():
    hub = AssistantStreamHub()
    release = __import__("threading").Event()

    def producer():
        yield {"type": "trace", "activity": []}
        release.wait(2)
        yield {"type": "final", "answer": "done"}

    first = hub.start("same", producer)
    second = hub.start("same", lambda: iter(()))
    assert first is second
    release.set()
    first._thread.join(2)
    assert first.done
    hub.forget("same")
    assert hub.get("same") is None


def test_reconnect_sequence_keeps_increasing_after_buffer_eviction():
    hub = AssistantStreamHub()
    count = MAX_EVENTS + 5
    job = hub.start("long-turn", lambda: ({"type": "trace", "index": n} for n in range(count)))
    job._thread.join(2)
    assert job.done
    retained = list(job.subscribe(None))
    assert len(retained) == MAX_EVENTS
    sequences = [int(frame.splitlines()[0].removeprefix("id: ")) for frame in retained]
    assert sequences == list(range(6, count + 1))
    resumed = _frames("".join(job.subscribe(MAX_EVENTS + 2)))
    assert [event["index"] for event in resumed] == list(range(MAX_EVENTS + 2, count))


def test_finished_jobs_are_evicted_at_capacity() -> None:
    from forgeflow.api.stream_hub import AssistantStreamHub

    hub = AssistantStreamHub(retention_seconds=3600, max_jobs=2)
    first = hub.start("a", lambda: iter([{"type": "final"}]))
    first._run()  # synchronous completion for the capacity check
    second = hub.start("b", lambda: iter([{"type": "final"}]))
    second._run()
    third = hub.start("c", lambda: iter([{"type": "final"}]))
    assert hub.get("a") is None
    assert hub.get("b") is not None and hub.get("c") is not None
    third._run()


def test_capacity_refuses_when_all_jobs_are_active() -> None:
    import threading

    import pytest

    from forgeflow.api.stream_hub import AssistantStreamHub, StreamCapacityError
    gate = threading.Event()
    hub = AssistantStreamHub(retention_seconds=3600, max_jobs=1)
    def held():
        gate.wait(timeout=2)
        yield {"type": "final"}

    job = hub.start("a", held)
    with pytest.raises(StreamCapacityError):
        hub.start("b", lambda: iter(()))
    gate.set()
    job.cancel()


def test_turn_journal_replays_completed_turn_after_restart(tmp_path) -> None:
    from forgeflow.api.stream_hub import AssistantStreamHub, TurnJournal

    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal)
    job = hub.start("owner:case:turn", lambda: iter([{"type": "final", "answer": "ok"}]),
                    metadata={"owner": "owner", "case_id": "case", "turn_id": "turn"})
    for _ in range(100):
        if job.done:
            break
        import time
        time.sleep(0.01)
    assert job.done
    restarted = AssistantStreamHub(journal=TurnJournal(tmp_path / "journal"))
    restored = restarted.get("owner:case:turn")
    assert restored is not None
    frames = list(restored.subscribe(None))
    assert any('"answer": "ok"' in frame for frame in frames)
    assert restored.metadata["owner"] == "owner"


def test_turn_journal_does_not_resume_an_inflight_turn(tmp_path) -> None:
    import threading

    from forgeflow.api.stream_hub import AssistantStreamHub, TurnJournal

    gate = threading.Event()
    hub = AssistantStreamHub(journal=TurnJournal(tmp_path / "journal"))
    job = hub.start("owner:case:inflight", lambda: (gate.wait(1), iter([{"type": "final"}]))[1])
    restarted = AssistantStreamHub(journal=TurnJournal(tmp_path / "journal"))
    assert restarted.get("owner:case:inflight") is None
    gate.set()
    job.cancel()

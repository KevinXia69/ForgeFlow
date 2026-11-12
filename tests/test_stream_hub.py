from __future__ import annotations

import json
import threading

import pytest

from forgeflow.api.stream_hub import MAX_EVENTS, AssistantStreamHub, StreamJob, TurnJournal


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


@pytest.mark.parametrize("cancel_from", ["original", "restored", "unloaded"])
def test_cancelling_a_completed_turn_revokes_its_saved_answer(tmp_path, cancel_from):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal)
    key = "owner:case:completed-turn"
    job = hub.start(key, lambda: iter([{"type": "final", "answer": "revoked answer"}]))
    job._thread.join(2)
    assert job.done
    if cancel_from != "original":
        hub = AssistantStreamHub(journal=journal)
        if cancel_from == "restored":
            assert hub.get(key) is not None
    hub.cancel(key)

    restored = AssistantStreamHub(journal=journal).get(key)
    assert restored is not None and restored.cancelled
    assert list(restored.subscribe(None)) == []
    assert journal.load(key)["events"] == []


def test_stop_during_final_persistence_wins_after_restart(tmp_path):
    """Stop may race with a final already received by the browser."""
    saving_final, release_save, stopped = threading.Event(), threading.Event(), threading.Event()

    class DelayedJournal(TurnJournal):
        def save(self, record):
            if record["status"] == "complete":
                saving_final.set()
                assert release_save.wait(2)
            super().save(record)

    journal = DelayedJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal)
    key = "owner:case:racing-turn"
    job = hub.start(key, lambda: iter([{"type": "final", "answer": "late final"}]),
                    on_cancel=stopped.set)
    assert saving_final.wait(2)
    cancel = threading.Thread(target=hub.cancel, args=(key,))
    cancel.start()
    try:
        assert stopped.wait(2)
    finally:
        release_save.set()
        cancel.join(2)
        job._thread.join(2)
    assert not cancel.is_alive() and not job._thread.is_alive()
    restored = AssistantStreamHub(journal=journal).get(key)
    assert restored is not None and restored.cancelled
    assert list(restored.subscribe(None)) == []


def test_cancelled_turn_stays_revoked_when_the_producer_finishes_later(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def producer():
        entered.set()
        assert release.wait(2)
        yield {"type": "final", "answer": "late final"}

    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal)
    key = "owner:case:late-turn"
    job = hub.start(key, producer)
    assert entered.wait(2)
    try:
        hub.cancel(key)
        restored = AssistantStreamHub(journal=journal).get(key)
        assert restored is not None and restored.cancelled
    finally:
        release.set()
        job._thread.join(2)
    restored = AssistantStreamHub(journal=journal).get(key)
    assert restored is not None and restored.cancelled
    assert list(restored.subscribe(None)) == []


def test_abandoned_turn_is_cancelled_after_the_grace(monkeypatch) -> None:
    """Nobody listening any more must not mean the turn keeps running."""
    import threading
    import time

    from forgeflow.api.stream_hub import AssistantStreamHub, StreamJob

    monkeypatch.setattr(StreamJob, "ORPHAN_GRACE", 0.2)
    stopped, release = threading.Event(), threading.Event()

    def producer():
        yield {"type": "trace", "activity": []}
        while not release.wait(0.01):
            if stopped.is_set():
                return
        yield {"type": "final", "answer": "late"}

    hub = AssistantStreamHub()
    job = hub.start("owner:case:orphan", producer, on_cancel=stopped.set)
    stream = job.subscribe(None)
    next(stream)
    stream.close()
    deadline = time.monotonic() + 2
    while not stopped.is_set() and time.monotonic() < deadline:
        time.sleep(0.02)
    release.set()
    assert stopped.is_set() and job.cancelled


def test_reconnecting_inside_the_grace_keeps_the_turn(monkeypatch) -> None:
    """A refresh reconnects within the grace; the turn must survive that."""
    import threading
    import time

    from forgeflow.api.stream_hub import AssistantStreamHub, StreamJob

    monkeypatch.setattr(StreamJob, "ORPHAN_GRACE", 0.4)
    stopped, release = threading.Event(), threading.Event()

    def producer():
        yield {"type": "trace", "activity": []}
        while not release.wait(0.01):
            if stopped.is_set():
                return
        yield {"type": "final", "answer": "kept"}

    hub = AssistantStreamHub()
    job = hub.start("owner:case:resume", producer, on_cancel=stopped.set)
    first = job.subscribe(None)
    next(first)
    first.close()
    time.sleep(0.05)
    again = job.subscribe(None)
    next(again)
    time.sleep(0.6)
    try:
        assert not stopped.is_set() and not job.cancelled
    finally:
        again.close()
        release.set()


def test_on_create_runs_only_when_the_job_is_created():
    hub = AssistantStreamHub()
    calls = []

    def callback():
        calls.append("created")

    release = threading.Event()

    def producer():
        yield {"type": "trace", "activity": []}
        release.wait(2)
        yield {"type": "final", "answer": "done"}

    first = hub.start("same", producer, on_create=callback)
    second = hub.start("same", producer, on_create=callback)
    assert first is second
    assert calls == ["created"]
    release.set()
    first._thread.join(2)


def test_early_cancel_records_are_bounded_by_capacity(tmp_path):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(max_jobs=2, journal=journal)
    for index in range(8):
        assert hub.cancel(f"early-{index}") is True
    assert len(hub._jobs) == 0
    assert len(list(journal.root.glob("*.json"))) <= 2


def test_restoring_completed_turns_obeys_memory_capacity(tmp_path):
    journal = TurnJournal(tmp_path / "journal", max_records=10)
    for index in range(5):
        job = StreamJob(f"done-{index}", (), on_finish=journal.save)
        job._done = True
        journal.save(job.snapshot())
    hub = AssistantStreamHub(max_jobs=2, journal=journal)
    restored = [hub.get(f"done-{index}") for index in range(5)]
    assert all(item is not None for item in restored)
    assert len(hub._jobs) == 2


def test_cancelled_worker_keeps_the_execution_slot_until_it_exits(tmp_path):
    from forgeflow.api.stream_hub import StreamCapacityError

    hub = AssistantStreamHub(max_jobs=1)
    release = threading.Event()

    def producer():
        release.wait(2)
        yield {"type": "final", "answer": "done"}

    job = hub.start("cancelled", producer)
    hub.cancel("cancelled")
    with pytest.raises(StreamCapacityError):
        hub.start("next", lambda: iter([]))
    release.set()
    job._thread.join(2)
    next_job = hub.start("next", lambda: iter([{"type": "final", "answer": "next"}]))
    assert next_job is not None


def test_expired_journal_record_is_not_restored(tmp_path):
    import time

    journal = TurnJournal(tmp_path / "journal", retention_seconds=1)
    job = StreamJob("expired", (), on_finish=journal.save)
    job._done = True
    record = job.snapshot()
    record["updated_at"] = time.time() - 2
    journal.save(record)
    hub = AssistantStreamHub(journal=journal)
    assert hub.get("expired") is None
    assert not journal._path("expired").exists()


def test_restored_turn_inherits_its_original_age(tmp_path):
    import time

    journal = TurnJournal(tmp_path / "journal", retention_seconds=100)
    job = StreamJob("aged", (), on_finish=journal.save)
    job._done = True
    record = job.snapshot()
    record["updated_at"] = time.time() - 60
    journal.save(record)
    hub = AssistantStreamHub(retention_seconds=100, journal=journal)
    assert hub.get("aged") is not None
    age = time.monotonic() - hub._finished_at["aged"]
    assert age > 50


def test_early_cancel_blocks_a_later_start_without_running_the_producer(tmp_path):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal)
    assert hub.cancel("racing-turn") is True
    started = []
    job = hub.start("racing-turn", lambda: started.append("started") or iter([]))
    assert job.cancelled is True
    assert started == []


def test_completion_timestamp_not_first_access_drives_expiry(tmp_path):
    import time

    hub = AssistantStreamHub(retention_seconds=50)
    job = hub.start("idle", lambda: iter([{"type": "final", "answer": "old"}]))
    job._thread.join(2)
    assert job.done and job.finished_at is not None
    job._finished_monotonic = time.monotonic() - 100
    hub._purge_locked()
    assert hub.get("idle") is None


def test_break_closes_the_suspended_generator():
    import threading
    import time

    started, release, closed = threading.Event(), threading.Event(), threading.Event()

    def producer():
        try:
            yield {"type": "trace"}
            started.set()
            release.wait(2)
            yield {"type": "trace"}
        finally:
            closed.set()

    job = StreamJob("close-me", producer())
    job.start()
    assert started.wait(1)
    job.cancel()
    release.set()
    assert closed.wait(1)
    for _ in range(20):
        if job.done:
            break
        time.sleep(0.01)
    assert job.done


def test_delayed_start_reuses_a_complete_journal_record(tmp_path):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(max_jobs=1, journal=journal)
    calls = []

    def producer():
        calls.append("run")
        yield {"type": "final", "answer": "done"}

    first = hub.start("same-turn", producer, request_hash="same")
    first._thread.join(2)
    assert first.done and calls == ["run"]
    # Simulate memory eviction while the complete journal record remains.
    hub._jobs.clear()
    hub._finished_at.clear()
    created = []
    second = hub.start("same-turn", lambda: calls.append("again"), request_hash="same",
                       reuse_completed=True, on_create=lambda: created.append(True))
    assert second.done and calls == ["run"] and created == []
    assert second.cancelled is False
    assert any(event.get("type") == "final" for _, event in second._events)


def test_legacy_repeat_recomputes_even_when_a_complete_record_exists(tmp_path):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(max_jobs=1, journal=journal)
    calls = []

    def first_producer():
        calls.append("first")
        yield {"type": "final", "answer": "first"}

    first = hub.start("legacy-key", first_producer, request_hash="same")
    first._thread.join(2)
    assert first.done
    hub._jobs.clear()
    hub._finished_at.clear()

    def second_producer():
        calls.append("second")
        yield {"type": "final", "answer": "second"}

    second = hub.start("legacy-key", second_producer, request_hash="same", reuse_completed=False)
    second._thread.join(2)
    assert calls == ["first", "second"]
    assert second.done

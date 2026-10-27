"""Bounded early Stops retain revocation until their fixed request expiry."""
from __future__ import annotations

import threading
from types import SimpleNamespace

import pytest

from forgeflow.api import stream_hub
from forgeflow.api.stream_hub import AssistantStreamHub, StreamCapacityError, TurnJournal


@pytest.fixture
def clock(monkeypatch):
    value = [1000.0]
    monkeypatch.setattr(stream_hub, "time", SimpleNamespace(
        time=lambda: value[0], monotonic=lambda: value[0],
    ))
    return value


def test_early_stop_owner_and_global_limits_survive_restart(tmp_path, clock):
    journal = TurnJournal(tmp_path / "journal", retention_seconds=100)
    options = dict(journal=journal, max_jobs=2, max_early_cancellations=3,
                   max_early_cancellations_per_owner=2)
    hub = AssistantStreamHub(**options)
    for key in ("a:1", "a:2"):
        assert hub.cancel(key, owner="a", expires_at=1100)
    with pytest.raises(StreamCapacityError, match="cancel_capacity"):
        hub.cancel("a:3", owner="a", expires_at=1100)
    assert journal.load("a:3") is None
    assert hub.cancel("b:1", owner="b", expires_at=1100)
    assert len(hub._jobs) <= 2
    restarted = AssistantStreamHub(**options)
    with pytest.raises(StreamCapacityError, match="cancel_capacity"):
        restarted.cancel("c:1", owner="c", expires_at=1100)
    assert len(list(journal.root.glob("*.json"))) == 3
    for key in ("a:1", "a:2", "b:1"):
        assert restarted.get(key).cancelled
        assert len(restarted._jobs) <= 2
    assert journal.early_cancel_counts("a") == (3, 2)
    # A duplicate Stop is idempotent and cannot extend the original lifetime.
    clock[0] += 5
    assert restarted.cancel("a:1", owner="a", expires_at=1200)
    assert journal.load("a:1")["expires_at"] == 1100


def test_full_early_stop_quota_does_not_block_an_existing_running_stop(tmp_path, clock):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal, max_jobs=1,
                             max_early_cancellations=1, max_early_cancellations_per_owner=1)
    assert hub.cancel("early", owner="a", expires_at=1100)
    entered, release, stopped = threading.Event(), threading.Event(), threading.Event()

    def producer():
        entered.set()
        release.wait(2)
        return iter(())

    # Creating the producer itself must not block the hub's admission lock.
    def events():
        yield from producer()

    job = hub.start("running", events, on_cancel=stopped.set,
                    metadata={"owner": "a", "expires_at": 1100})
    assert entered.wait(2)
    try:
        assert hub.cancel("running", owner="a", expires_at=900)
        assert stopped.is_set() and job.cancelled
        assert journal.early_cancel_counts("a") == (1, 1)
        assert len(hub._jobs) <= 1
        assert hub.get("early").cancelled
        assert len(hub._jobs) <= 1
    finally:
        release.set()
        job._thread.join(2)


def test_no_journal_never_evicts_an_unexpired_early_stop(clock):
    hub = AssistantStreamHub(max_jobs=1, retention_seconds=10)
    assert hub.cancel("old", owner="a", expires_at=1100)
    created = []

    def producer():
        created.append(True)
        return iter(())

    with pytest.raises(StreamCapacityError):
        hub.start("new", producer)
    with pytest.raises(StreamCapacityError):
        hub.cancel("new", owner="b", expires_at=1100)
    clock[0] += 20  # Cache retention alone cannot remove the revocation.
    assert hub.start("old", producer).cancelled
    assert hub.get("old").cancelled
    assert created == []


def test_concurrent_early_stops_cannot_overbook_the_global_allowance(tmp_path, clock):
    hub = AssistantStreamHub(journal=TurnJournal(tmp_path / "journal"),
                             max_early_cancellations=1)
    ready = threading.Barrier(3)
    results = []

    def cancel(key):
        ready.wait(2)
        try:
            results.append(hub.cancel(key, owner=key, expires_at=1100))
        except StreamCapacityError:
            results.append(False)

    callers = [threading.Thread(target=cancel, args=(key,)) for key in ("a", "b")]
    for caller in callers:
        caller.start()
    ready.wait(2)
    for caller in callers:
        caller.join(2)
        assert not caller.is_alive()
    assert sorted(results) == [False, True]
    assert len(list(hub._journal.root.glob("*.json"))) == 1


def test_cancel_only_traffic_purges_expired_memory_and_disk_without_starting(tmp_path, clock):
    journal = TurnJournal(tmp_path / "journal", retention_seconds=10)
    hub = AssistantStreamHub(journal=journal, retention_seconds=10, max_jobs=1,
                             max_early_cancellations=1, max_early_cancellations_per_owner=1)
    before = threading.active_count()
    assert hub.cancel("old", owner="a", expires_at=1010)
    clock[0] = 1011
    assert hub.cancel("new", owner="a", expires_at=1020)
    assert list(hub._jobs) == ["new"]
    assert len(list(journal.root.glob("*.json"))) == 1
    assert journal.load("old") is None
    assert hub.get("old") is None
    with pytest.raises(ValueError, match="turn_expired"):
        hub.start("old", lambda: pytest.fail("expired turn started"),
                  metadata={"expires_at": 1010})
    with pytest.raises(ValueError, match="turn_expired"):
        hub.cancel("old", owner="a", expires_at=1010)
    assert threading.active_count() == before


def test_journal_load_expires_legacy_records_without_purge_or_ttl_refresh(tmp_path, clock):
    journal = TurnJournal(tmp_path / "journal", retention_seconds=10)
    journal.save({"key": "expired", "status": "cancelled", "updated_at": 980,
                  "events": [], "metadata": {}})
    journal.save({"key": "legacy", "status": "cancelled", "updated_at": 995,
                  "events": [], "metadata": {}})
    assert journal.load("expired") is None
    assert not journal._path("expired").exists()
    first = AssistantStreamHub(journal=journal)
    assert first.get("legacy").expires_at == 1005
    clock[0] = 1004
    second = AssistantStreamHub(journal=journal)
    assert second.get("legacy").expires_at == 1005
    assert second.cancel("legacy", owner="a", expires_at=1200)
    clock[0] = 1006
    assert second.get("legacy") is None
    assert journal.load("legacy") is None


def test_journal_replay_does_not_exceed_a_cache_full_of_active_jobs(tmp_path, clock):
    journal = TurnJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal, max_jobs=1)
    entered, release = threading.Event(), threading.Event()

    def events():
        entered.set()
        release.wait(2)
        yield {"type": "final", "answer": "live"}

    active = hub.start("active", events)
    assert entered.wait(2)
    try:
        assert hub.cancel("early", owner="a", expires_at=1100)
        assert list(hub._jobs) == ["active"]
        for _ in range(3):
            assert hub.get("early").cancelled
            assert list(hub._jobs) == ["active"]
    finally:
        release.set()
        active._thread.join(2)


def test_memory_eviction_cannot_forget_a_stop_before_it_is_persisted(tmp_path, clock):
    saving, persist, release = threading.Event(), threading.Event(), threading.Event()

    class SlowJournal(TurnJournal):
        def save(self, record):
            if record["status"] == "cancelled":
                saving.set()
                assert persist.wait(2)
            super().save(record)

    journal = SlowJournal(tmp_path / "journal")
    hub = AssistantStreamHub(journal=journal, max_jobs=1, retention_seconds=1)

    def events():
        release.wait(2)
        yield {"type": "final", "answer": "old"}

    job = hub.start("old", events)
    stopping = threading.Thread(target=hub.cancel, args=("old",))
    stopping.start()
    assert saving.wait(2)
    assert hub.get("old").cancelled
    clock[0] += 2  # Even cache TTL cannot erase a Stop before its journal save.
    try:
        with pytest.raises(StreamCapacityError):
            hub.start("new", lambda: pytest.fail("unpersisted cancellation was evicted"))
        assert hub.get("old").cancelled
    finally:
        persist.set()
        release.set()
        stopping.join(2)
        job._thread.join(2)
    assert AssistantStreamHub(journal=journal).get("old").cancelled

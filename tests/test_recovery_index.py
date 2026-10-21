"""Tests for stable recovery prepare keys and their durable index."""

from __future__ import annotations

import json
import stat
import threading

import pytest

from forgeflow.recovery.index import (
    IndexConflict,
    PrepareIndex,
    business_snapshot_digest,
    prepare_key,
)

RUN_ID = "run-1"
SOURCES, POLICY, KEY = "a" * 64, "b" * 64, "c" * 64


def _snapshot() -> dict:
    return {
        "schema_version": 1,
        "collected_at_utc": "2026-10-21T00:00:00+00:00",
        "models": {
            "product.product": [
                {"id": 2, "name": "B", "audit": {"collected_at_utc": "first"}},
                {"id": 1, "name": "A"},
            ],
            "sale.order": [],
        },
        "scope": {"target_product_code": "SKU-1", "sales_order_ids": [7]},
    }


def _key(**overrides) -> str:
    values = {
        "run_id": RUN_ID, "request": {"sales_order_ids": [7]}, "snapshot": _snapshot(),
        "sources_sha256": SOURCES, "policy_sha256": POLICY,
    }
    values.update(overrides)
    return prepare_key(**values)


def test_snapshot_digest_ignores_timestamps_and_row_order():
    expected = business_snapshot_digest(_snapshot())
    changed = _snapshot()
    changed["collected_at_utc"] = "2026-10-22T00:00:00+00:00"
    changed["models"]["product.product"][0]["audit"]["collected_at_utc"] = "second"
    assert business_snapshot_digest(changed) == expected
    changed["models"]["product.product"].reverse()
    assert business_snapshot_digest(changed) == expected
    changed["models"]["product.product"][1]["name"] = "changed"
    assert business_snapshot_digest(changed) != expected


@pytest.mark.parametrize("bad", [None, [], "snapshot", 1])
def test_snapshot_digest_rejects_non_object(bad):
    with pytest.raises(ValueError):
        business_snapshot_digest(bad)


@pytest.mark.parametrize("row", [{"name": "missing"}, {"id": "2"}, {"id": 2.0}])
def test_snapshot_digest_rejects_rows_without_integer_id(row):
    snapshot = _snapshot()
    snapshot["models"]["product.product"] = [row]
    with pytest.raises(ValueError):
        business_snapshot_digest(snapshot)


def test_prepare_key_is_stable_and_sensitive_to_every_input():
    expected = _key()
    assert _key() == expected
    retimed = _snapshot()
    retimed["collected_at_utc"] = "2026-10-23T00:00:00+00:00"
    assert _key(snapshot=retimed) == expected
    assert _key(run_id="run-2") != expected
    assert _key(request={"sales_order_ids": [8]}) != expected
    changed = _snapshot()
    changed["scope"]["target_product_code"] = "SKU-2"
    assert _key(snapshot=changed) != expected
    assert _key(sources_sha256="d" * 64) != expected
    assert _key(policy_sha256="e" * 64) != expected


@pytest.mark.parametrize("run_id", ["", "../run", "run 1", "run/1", None])
def test_prepare_key_rejects_bad_run_id(run_id):
    with pytest.raises(ValueError):
        _key(run_id=run_id)


@pytest.mark.parametrize("name", ["sources_sha256", "policy_sha256"])
@pytest.mark.parametrize("digest", ["a" * 63, "a" * 65, "z" * 64, "A" * 64, None])
def test_prepare_key_rejects_bad_digest(name, digest):
    with pytest.raises(ValueError):
        _key(**{name: digest})


def _index(tmp_path) -> PrepareIndex:
    return PrepareIndex(tmp_path / "index")


def test_index_round_trip_and_permissions(tmp_path):
    index = _index(tmp_path)
    index.put(KEY, "plan-1")
    assert index.get(KEY) == "plan-1"
    entry = index.root / f"{KEY}.json"
    assert json.loads(entry.read_text()) == {
        "schema_version": 1,
        "key": KEY,
        "plan_id": "plan-1",
    }
    assert stat.S_IMODE(entry.stat().st_mode) == 0o600
    assert stat.S_IMODE(index.root.stat().st_mode) == 0o700


def test_index_same_mapping_is_noop_and_conflict_preserves_entry(tmp_path):
    index = _index(tmp_path)
    index.put(KEY, "plan-1")
    entry = index.root / f"{KEY}.json"
    before = entry.read_bytes()
    index.put(KEY, "plan-1")
    assert entry.read_bytes() == before
    with pytest.raises(IndexConflict):
        index.put(KEY, "plan-2")
    assert index.get(KEY) == "plan-1"


def test_index_get_returns_none_for_missing_corrupt_and_foreign_entries(tmp_path):
    index = _index(tmp_path)
    assert index.get(KEY) is None
    entry = index.root / f"{KEY}.json"
    entry.write_text("{not json")
    assert index.get(KEY) is None
    entry.write_text(json.dumps({"schema_version": 2, "key": KEY, "plan_id": "plan-1"}))
    assert index.get(KEY) is None
    entry.write_text(json.dumps({"schema_version": 1, "key": KEY, "plan_id": "plan-1"}))
    assert index.get(KEY) == "plan-1"


def test_index_get_does_not_follow_symlinks(tmp_path):
    index = _index(tmp_path)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"schema_version": 1, "key": KEY, "plan_id": "plan-1"}))
    (index.root / f"{KEY}.json").symlink_to(outside)
    assert index.get(KEY) is None


def test_index_concurrent_conflicting_puts_have_one_winner(tmp_path):
    index = _index(tmp_path)
    barrier = threading.Barrier(2)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(plan_id: str) -> None:
        barrier.wait()
        try:
            index.put(KEY, plan_id)
            outcome = "ok"
        except IndexConflict:
            outcome = "conflict"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(p,)) for p in ("plan-1", "plan-2")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert sorted(outcomes) == ["conflict", "ok"]
    assert index.get(KEY) in {"plan-1", "plan-2"}

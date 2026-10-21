from __future__ import annotations

import json

import pytest

from forgeflow.worker.protocol import (
    JobSpec,
    MemoryWorkerQueue,
    ProtocolError,
    Result,
    WorkerHello,
    decode,
    encode,
)


def test_messages_round_trip_and_validate():
    hello = WorkerHello("worker-1", capacity=2, labels=("sg",))
    line = encode(hello)
    assert line.endswith(b"\n")
    payload = decode(line)
    assert payload["type"] == "WorkerHello"
    assert payload["worker_id"] == "worker-1"
    with pytest.raises(ProtocolError):
        decode(b"{}\n")
    with pytest.raises(ProtocolError):
        WorkerHello("")
    with pytest.raises(ProtocolError):
        JobSpec("case", "run", agent_timeout_sec=5)


def test_claim_is_exclusive_until_expiry_then_reclaimable():
    now = [1000.0]
    queue = MemoryWorkerQueue(_clock=lambda: now[0])
    queue.submit(JobSpec("case-1", "run-1"))
    hello = WorkerHello("worker-1")
    lease = queue.claim(hello)
    assert lease is not None
    assert queue.claim(WorkerHello("worker-2")) is None
    now[0] += 61
    second = queue.claim(WorkerHello("worker-2"))
    assert second is not None
    assert second.job.case_id == "case-1"
    assert second.lease_id != lease.lease_id


def test_result_is_single_use_and_owner_scoped():
    queue = MemoryWorkerQueue()
    queue.submit(JobSpec("case-1", "run-1"))
    lease = queue.claim(WorkerHello("worker-1"))
    assert lease is not None
    result = Result(lease.lease_id, "succeeded", "ok", 100.0)
    assert queue.complete(result, "worker-1") == result
    assert queue.complete(result, "worker-1") == result
    assert queue.claim(WorkerHello("worker-2")) is None
    with pytest.raises(ProtocolError):
        queue.complete(result, "worker-2")


def test_heartbeat_and_expiry_rules():
    now = [1000.0]
    queue = MemoryWorkerQueue(_clock=lambda: now[0])
    queue.submit(JobSpec("case-1", "run-1"))
    lease = queue.claim(WorkerHello("worker-1"))
    assert lease is not None
    now[0] += 1
    renewed = queue.heartbeat(lease.lease_id, "worker-1")
    assert renewed.expires_at > lease.expires_at
    with pytest.raises(ProtocolError):
        queue.heartbeat(lease.lease_id, "worker-2")
    now[0] += 61
    with pytest.raises(ProtocolError):
        queue.heartbeat(lease.lease_id, "worker-1")


def test_duplicate_job_submission_does_not_duplicate_lease():
    queue = MemoryWorkerQueue()
    queue.submit(JobSpec("case-1", "run-1"))
    queue.submit(JobSpec("case-1", "run-2"))
    lease = queue.claim(WorkerHello("worker-1"))
    assert lease is not None
    assert lease.job.run_id == "run-2"


def test_result_validation():
    with pytest.raises(ProtocolError):
        Result("lease", "done")
    with pytest.raises(ProtocolError):
        Result("lease", "failed", verifier_score=101)
    with pytest.raises(ProtocolError):
        decode(json.dumps({"schema_version": 2, "type": "Result"}) + "\n")

"""A small outbound-worker protocol with no network or server dependency.

The real worker will poll a control API. This module fixes the message shape and
lease semantics so the API, worker and tests cannot drift: one active lease per
job, expiry allows reclaim, and a result is accepted only for the current lease.
"""

from __future__ import annotations

import json
import secrets
import time
from dataclasses import asdict, dataclass, field
from typing import Any

SCHEMA_VERSION = 1


class ProtocolError(ValueError):
    """A stable protocol failure code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _text(value: object, name: str, limit: int = 120) -> str:
    if not isinstance(value, str) or not value or len(value) > limit:
        raise ProtocolError(f"invalid_{name}")
    return value


@dataclass(frozen=True, slots=True)
class WorkerHello:
    worker_id: str
    capacity: int = 1
    labels: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _text(self.worker_id, "worker_id")
        if type(self.capacity) is not int or not 1 <= self.capacity <= 8:
            raise ProtocolError("invalid_capacity")
        if len(self.labels) > 16 or any(not isinstance(x, str) or not x for x in self.labels):
            raise ProtocolError("invalid_labels")


@dataclass(frozen=True, slots=True)
class JobSpec:
    case_id: str
    run_id: str
    scenario: str = "A"
    agent_timeout_sec: int = 600

    def __post_init__(self) -> None:
        _text(self.case_id, "case_id")
        _text(self.run_id, "run_id")
        _text(self.scenario, "scenario", 40)
        if type(self.agent_timeout_sec) is not int or not 30 <= self.agent_timeout_sec <= 3600:
            raise ProtocolError("invalid_timeout")


@dataclass(frozen=True, slots=True)
class Lease:
    lease_id: str
    worker_id: str
    job: JobSpec
    expires_at: float


@dataclass(frozen=True, slots=True)
class Result:
    lease_id: str
    state: str
    detail: str = ""
    verifier_score: float | None = None

    def __post_init__(self) -> None:
        _text(self.lease_id, "lease_id")
        if self.state not in ("succeeded", "failed", "execution_unknown"):
            raise ProtocolError("invalid_result_state")
        if not isinstance(self.detail, str) or len(self.detail) > 2000:
            raise ProtocolError("invalid_detail")
        if self.verifier_score is not None and not 0 <= self.verifier_score <= 100:
            raise ProtocolError("invalid_score")


def encode(message: object) -> bytes:
    """Encode a protocol message as one canonical JSON line."""
    payload = {"schema_version": SCHEMA_VERSION, "type": type(message).__name__, **asdict(message)}
    return (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()


def decode(line: bytes | str) -> dict[str, Any]:
    """Decode and minimally validate a protocol line."""
    if isinstance(line, bytes):
        try:
            text = line.decode("utf-8")
        except UnicodeDecodeError:
            raise ProtocolError("invalid_utf8") from None
    else:
        text = line
    if not text.endswith("\n") or len(text) > 65536:
        raise ProtocolError("invalid_frame")
    try:
        payload = json.loads(text)
    except ValueError:
        raise ProtocolError("invalid_json") from None
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ProtocolError("invalid_schema")
    if not isinstance(payload.get("type"), str):
        raise ProtocolError("invalid_type")
    return payload


@dataclass
class MemoryWorkerQueue:
    """Fake control plane used by tests; deliberately not a network server."""

    jobs: dict[str, JobSpec] = field(default_factory=dict)
    leases: dict[str, Lease] = field(default_factory=dict)
    results: dict[str, Result] = field(default_factory=dict)
    _clock: Any = field(default=time.time, repr=False)

    def submit(self, job: JobSpec) -> None:
        self.jobs[job.case_id] = job

    def claim(self, hello: WorkerHello) -> Lease | None:
        now = self._clock()
        for case_id, job in sorted(self.jobs.items()):
            if case_id in self.results or any(
                lease.job.case_id == case_id and lease.expires_at > now
                for lease in self.leases.values()
            ):
                continue
            lease = Lease(
                lease_id=secrets.token_hex(16),
                worker_id=hello.worker_id,
                job=job,
                expires_at=now + 60,
            )
            self.leases[lease.lease_id] = lease
            return lease
        return None

    def heartbeat(self, lease_id: str, worker_id: str) -> Lease:
        lease = self.leases.get(lease_id)
        if lease is None or lease.worker_id != worker_id:
            raise ProtocolError("unknown_lease")
        if lease.expires_at <= self._clock():
            raise ProtocolError("lease_expired")
        renewed = Lease(lease_id, worker_id, lease.job, self._clock() + 60)
        self.leases[lease_id] = renewed
        return renewed

    def complete(self, result: Result, worker_id: str) -> Result:
        lease = self.leases.get(result.lease_id)
        if lease is None or lease.worker_id != worker_id:
            raise ProtocolError("unknown_lease")
        if lease.expires_at <= self._clock():
            raise ProtocolError("lease_expired")
        if result.lease_id in self.results:
            return self.results[result.lease_id]
        self.results[result.lease_id] = result
        self.jobs.pop(lease.job.case_id, None)
        return result

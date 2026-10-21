"""One-time approval tokens that bind a prepared plan to one authorized run.

A plan can only be executed through an approval that names the run, the plan,
the policy and the snapshot it was prepared from, the approver and an expiry.
Consumption is exclusive: the first ``consume`` creates a marker with
``O_EXCL``, so a replay, a double click, or a second worker cannot execute the
same plan twice.

The token store is a local directory owned by the executor. It is not an
authentication boundary against a process that already has the same filesystem
privileges; it is a correctness protocol between the product API and the
executor, and the executor must live behind a credential boundary (see the
permissions profile) rather than beside the agent.
"""

from __future__ import annotations

import hashlib
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .recovery.storage import StorageError, atomic_json, read_json, sync_directory

SCHEMA_VERSION = 1
PLAN_ID = re.compile(r"[a-f0-9]{64}")
SHA256 = re.compile(r"[a-f0-9]{64}")
NONCE = re.compile(r"[a-f0-9]{32}")
APPROVER = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,119}")
MAX_TTL_SECONDS = 24 * 60 * 60


class ApprovalError(RuntimeError):
    pass


@dataclass(frozen=True)
class Approval:
    """An immutable binding between a plan and the authorization to execute it."""

    nonce: str
    run_id: str
    plan_id: str
    policy_sha256: str
    snapshot_sha256: str
    approver: str
    issued_at_utc: str
    expires_at_utc: str

    def payload(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "nonce": self.nonce,
            "run_id": self.run_id,
            "plan_id": self.plan_id,
            "policy_sha256": self.policy_sha256,
            "snapshot_sha256": self.snapshot_sha256,
            "approver": self.approver,
            "issued_at_utc": self.issued_at_utc,
            "expires_at_utc": self.expires_at_utc,
        }

    def digest(self) -> str:
        canonical = (
            "|".join([
                str(SCHEMA_VERSION), self.nonce, self.run_id, self.plan_id,
                self.policy_sha256, self.snapshot_sha256, self.approver,
                self.issued_at_utc, self.expires_at_utc,
            ])
        ).encode()
        return hashlib.sha256(canonical).hexdigest()


def _now(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        raise ApprovalError("approval_clock_requires_timezone")
    return value.astimezone(UTC)


def _check_hash(name: str, value: str) -> str:
    if not isinstance(value, str) or not SHA256.fullmatch(value):
        raise ApprovalError(f"invalid_{name}")
    return value


def issue(
    store: str | Path,
    *,
    run_id: str,
    plan_id: str,
    policy_sha256: str,
    snapshot_sha256: str,
    approver: str,
    ttl_seconds: int = 900,
    now: datetime | None = None,
    nonce: str | None = None,
) -> Approval:
    """Create a single-use approval for one prepared plan and persist it."""
    if not isinstance(run_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,119}", run_id):
        raise ApprovalError("invalid_run_id")
    if not isinstance(plan_id, str) or not PLAN_ID.fullmatch(plan_id):
        raise ApprovalError("invalid_plan_id")
    _check_hash("policy_sha256", policy_sha256)
    _check_hash("snapshot_sha256", snapshot_sha256)
    if not isinstance(approver, str) or not APPROVER.fullmatch(approver):
        raise ApprovalError("invalid_approver")
    if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or not 0 < ttl_seconds <= MAX_TTL_SECONDS:
        raise ApprovalError("invalid_ttl")
    issued = _now(now)
    if nonce is not None and (not isinstance(nonce, str) or not NONCE.fullmatch(nonce)):
        raise ApprovalError("invalid_approval")
    approval = Approval(
        nonce=os.urandom(16).hex() if nonce is None else nonce,
        run_id=run_id,
        plan_id=plan_id,
        policy_sha256=policy_sha256,
        snapshot_sha256=snapshot_sha256,
        approver=approver,
        issued_at_utc=issued.isoformat(),
        expires_at_utc=(issued + timedelta(seconds=ttl_seconds)).isoformat(),
    )
    directory = Path(store)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    if nonce is not None and os.path.lexists(directory / f"approval-{nonce}.json"):
        # Root's product decision lock persists an intent before this write.
        # A retry may retrieve exactly that token, never mint a replacement.
        _load(directory, approval)
        return approval
    for existing in sorted(directory.glob("approval-*.json")):
        try:
            payload = read_json(existing, 64 * 1024)
        except (OSError, StorageError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        if (payload.get("run_id") != run_id or payload.get("plan_id") != plan_id):
            continue
        try:
            existing_expiry = datetime.fromisoformat(str(payload["expires_at_utc"]))
        except (KeyError, ValueError):
            continue
        if existing_expiry.tzinfo is None or existing_expiry <= issued:
            continue
        nonce = str(payload.get("nonce", ""))
        if NONCE.fullmatch(nonce) and not (directory / f"approval-{nonce}.used").exists():
            raise ApprovalError("approval_exists")
    atomic_json(directory / f"approval-{approval.nonce}.json", approval.payload())
    sync_directory(directory)
    return approval


def _load(store: Path, approval: Approval) -> dict[str, Any]:
    if not NONCE.fullmatch(approval.nonce):
        raise ApprovalError("invalid_approval")
    try:
        payload = read_json(store / f"approval-{approval.nonce}.json", 64 * 1024)
    except (OSError, StorageError, ValueError) as error:
        raise ApprovalError("approval_not_found") from error
    if not isinstance(payload, dict) or payload != approval.payload():
        raise ApprovalError("approval_token_tampered")
    return payload


def used_digest(store: str | Path, approval: Approval) -> str | None:
    """Return the digest recorded when this approval was spent, if any."""
    if not NONCE.fullmatch(approval.nonce):
        return None
    path = Path(store) / f"approval-{approval.nonce}.used"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        with os.fdopen(fd, "rb") as handle:
            raw = handle.read(200).decode("ascii", errors="replace").strip()
    except OSError:
        return None
    return raw if SHA256.fullmatch(raw) else None


def consume(
    store: str | Path,
    approval: Approval,
    *,
    run_id: str,
    plan_id: str,
    policy_sha256: str,
    snapshot_sha256: str,
    approver: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Verify an approval and spend it exactly once.

    Raises :class:`ApprovalError` with a stable code on any mismatch, expiry, or
    replay. The marker is created only after every check passes, so a failed
    call does not burn a still-valid approval.
    """
    directory = Path(store)
    payload = _load(directory, approval)
    if approval.run_id != run_id:
        raise ApprovalError("approval_run_mismatch")
    if approval.plan_id != plan_id:
        raise ApprovalError("approval_plan_mismatch")
    if approval.policy_sha256 != policy_sha256:
        raise ApprovalError("approval_policy_mismatch")
    if approval.snapshot_sha256 != snapshot_sha256:
        raise ApprovalError("approval_snapshot_mismatch")
    if approval.approver != approver:
        raise ApprovalError("approval_approver_mismatch")
    try:
        expires = datetime.fromisoformat(approval.expires_at_utc)
    except ValueError as error:
        raise ApprovalError("approval_expiry_unreadable") from error
    if expires.tzinfo is None:
        raise ApprovalError("approval_expiry_requires_timezone")
    if _now(now) >= expires.astimezone(UTC):
        raise ApprovalError("approval_expired")
    marker = directory / f"approval-{approval.nonce}.used"
    # Write the digest to a private temp file first, then publish it with
    # link(), which fails if the marker exists. A crash can therefore never
    # leave an empty marker that would look consumed but be unresumable.
    temporary = directory / f".approval-{approval.nonce}.{os.getpid()}.tmp"
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        try:
            os.write(fd, (approval.digest() + "\n").encode())
            os.fsync(fd)
        except OSError as error:
            raise ApprovalError("approval_marker_write_failed") from error
        finally:
            os.close(fd)
        try:
            os.link(temporary, marker)
        except FileExistsError as error:
            raise ApprovalError("approval_already_consumed") from error
        except OSError as error:
            raise ApprovalError("approval_marker_write_failed") from error
    finally:
        # A failed write or link must not leave a temp file that blocks a retry.
        try:
            os.unlink(temporary)
        except OSError:
            pass
    sync_directory(directory)
    return payload

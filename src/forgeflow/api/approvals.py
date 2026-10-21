"""Product-side approval records for recorded recovery plans.

The executor already enforces single-use consumption; this layer adds what a
human reviewer needs: an identity, one live approval per plan, an explicit
revocation, and an append-only audit trail. The approval directory is created
root-side by the service operator and is never inside the agent workspace.
"""

from __future__ import annotations

import fcntl
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from forgeflow.approval import (
    NONCE,
    PLAN_ID,
    Approval,
    ApprovalError,
    consume,
    issue,
    used_digest,
)

DEFAULT_TTL_SECONDS = 900


class ApprovalUnavailable(Exception):
    """The plan cannot be approved, or the request conflicts with its state."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ApprovalService:
    """Filesystem-backed approvals under one root, grouped by run and plan."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        if self.root.is_symlink():
            raise RuntimeError("approvals root must not be a symlink")

    def _store(self, run_id: str, plan_id: str) -> Path:
        if PLAN_ID.fullmatch(plan_id) is None:
            raise ApprovalUnavailable("invalid_plan_id")
        if not run_id or "/" in run_id or run_id.startswith(".") or len(run_id) > 120:
            raise ApprovalUnavailable("invalid_run_id")
        return self.root / run_id / plan_id

    def _audit(self, entry: dict[str, Any]) -> None:
        try:
            self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
            with open(self.root / "audit.jsonl", "a", encoding="utf-8") as handle:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
                try:
                    handle.write(json.dumps({"at": _now().isoformat(), **entry}) + "\n")
                    handle.flush()
                finally:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            return

    def issue(
        self,
        *,
        run_id: str,
        plan_id: str,
        approver: str,
        policy_sha256: str | None,
        snapshot_sha256: str | None,
        ttl_seconds: int = DEFAULT_TTL_SECONDS,
    ) -> dict[str, Any]:
        if not isinstance(policy_sha256, str) or not isinstance(snapshot_sha256, str):
            raise ApprovalUnavailable("plan_not_approvable")
        try:
            approval = issue(
                self._store(run_id, plan_id),
                run_id=run_id,
                plan_id=plan_id,
                policy_sha256=policy_sha256,
                snapshot_sha256=snapshot_sha256,
                approver=approver,
                ttl_seconds=ttl_seconds,
            )
        except ApprovalError as error:
            raise ApprovalUnavailable(str(error)) from None
        self._audit({"action": "approve", "uid": approver, "run_id": run_id,
                     "plan_id": plan_id, "nonce": approval.nonce, "outcome": "issued"})
        return {**approval.payload(), "status": "valid"}

    def _load(self, store: Path, nonce: str) -> Approval | None:
        if NONCE.fullmatch(nonce) is None:
            return None
        try:
            payload = json.loads((store / f"approval-{nonce}.json").read_text())
            return Approval(**{name: payload[name] for name in (
                "nonce", "run_id", "plan_id", "policy_sha256", "snapshot_sha256",
                "approver", "issued_at_utc", "expires_at_utc")})
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def list(self, *, run_id: str, plan_id: str) -> list[dict[str, Any]]:
        store = self._store(run_id, plan_id)
        if not store.is_dir() or store.is_symlink():
            return []
        records: list[dict[str, Any]] = []
        for path in sorted(store.glob("approval-*.json")):
            if path.is_symlink() or not path.is_file():
                continue
            approval = self._load(store, path.stem.removeprefix("approval-"))
            if approval is None:
                continue
            expired = datetime.fromisoformat(approval.expires_at_utc) <= _now()
            revoked = (store / f"approval-{approval.nonce}.revoked").exists()
            used = used_digest(store, approval) is not None
            status = ("revoked" if revoked else "used" if used
                      else "expired" if expired else "valid")
            records.append({**approval.payload(), "status": status})
        return records

    def revoke(self, *, run_id: str, plan_id: str, nonce: str, uid: str) -> dict[str, Any]:
        store = self._store(run_id, plan_id)
        approval = self._load(store, nonce)
        if approval is None:
            raise ApprovalUnavailable("approval_not_found")
        mark = store / f"approval-{nonce}.revoked"
        try:
            if not mark.exists():
                mark.write_text(json.dumps({"at": _now().isoformat(), "uid": uid}) + "\n")
        except OSError:
            raise ApprovalUnavailable("approval_not_revocable") from None
        try:
            consume(
                store, approval,
                run_id=approval.run_id, plan_id=approval.plan_id,
                policy_sha256=approval.policy_sha256,
                snapshot_sha256=approval.snapshot_sha256,
                approver=approval.approver,
            )
        except ApprovalError:
            # Expired or already used approvals are still marked revoked; the
            # single-use marker is what the executor trusts.
            pass
        self._audit({"action": "revoke", "uid": uid, "run_id": run_id,
                     "plan_id": plan_id, "nonce": nonce, "outcome": "revoked"})
        return {"nonce": nonce, "status": "revoked"}

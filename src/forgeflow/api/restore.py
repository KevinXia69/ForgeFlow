"""Restore safety: a restored backup starts paused until an admin verifies it.

The operator writes a marker before restoring an old backup.  While it exists,
mutating API routes are refused (login and the verification route stay open).
Verification compares the recorded environment fingerprint with the live control
database and removes the marker only when it matches.

Because a fingerprint that only names the last effect/ticket id cannot prove the
*work* survived, ``pause`` can also capture the semantic identity of every
in-flight or business-write operation: kind, ticket, idempotency key and a hash
of its payload.  Verification then checks each captured operation one by one and
refuses a restored database that is missing one, changed one, or grew a new
unresolved business write.
"""
from __future__ import annotations

import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

#: Effect states that represent work whose outcome still matters.
UNRESOLVED_EFFECT_STATES = ("pending", "running")

#: Operations captured before a restore.  A business write is always included so
#: the last real ERP mutation is checked even when it already completed.
OPERATION_QUERY = (
    "SELECT id::text AS effect_id, ticket_id::text AS ticket_id, kind, state, "
    "side_effect_class, COALESCE(idempotency_key, '') AS idempotency_key, payload "
    "FROM control_effects "
    "WHERE state IN ('pending','running') OR side_effect_class = 'business_write' "
    "ORDER BY updated_at DESC, id DESC LIMIT %s"
)


def default_restore_marker(data_root: str | Path | None = None) -> Path:
    """The one default marker path used by both the CLI and the API.

    Deployment precedence: explicit ``FORGEFLOW_RESTORE_MARKER``, then the data
    root the API uses, then the historical ``runs`` directory.
    """
    configured = os.environ.get("FORGEFLOW_RESTORE_MARKER", "").strip()
    if configured:
        return Path(configured)
    root = data_root or os.environ.get("FORGEFLOW_DATA_ROOT") or "runs"
    return Path(root) / "RESTORE_REQUIRED"


class RestoreError(ValueError):
    def __init__(self, code: str, *, mismatched: list[str] | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.mismatched = mismatched or []


def _payload_digest(payload: Any) -> str:
    raw = json.dumps(payload if payload is not None else {}, sort_keys=True,
                     separators=(",", ":"), ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _connect(control_url: str | None):
    url = control_url or os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
    if not url:
        return None
    import psycopg

    return psycopg.connect(url, connect_timeout=5)


def operation_snapshot(control_url: str | None = None, *, limit: int = 20
                       ) -> list[dict[str, Any]]:
    """Capture the semantic identity of operations a restore must preserve."""
    try:
        connection = _connect(control_url)
    except Exception:  # noqa: BLE001 - an unreachable DB is reported as no snapshot
        return []
    if connection is None:
        return []
    try:
        with connection:
            rows = connection.execute(OPERATION_QUERY, (max(1, int(limit)),)).fetchall()
    except Exception:  # noqa: BLE001 - an unreachable DB is reported as no snapshot
        return []
    finally:
        connection.close()
    return [_operation_row(row) for row in rows]


def _operation_row(row) -> dict[str, Any]:
    effect_id, ticket_id, kind, state, side_effect_class, idempotency_key, payload = row
    return {
        "effect_id": str(effect_id),
        "ticket_id": str(ticket_id),
        "kind": str(kind),
        "state": str(state),
        "side_effect_class": str(side_effect_class or ""),
        "idempotency_key": str(idempotency_key or ""),
        "payload_sha256": _payload_digest(payload),
    }


def current_operations(control_url: str | None = None, *, limit: int = 20
                       ) -> list[dict[str, Any]]:
    """Read the same operation identities from the live (restored) database."""
    return operation_snapshot(control_url, limit=limit)


class RestoreGuard:
    def __init__(self, marker: str | Path) -> None:
        self.marker = Path(marker)

    def pause(self, *, reason: str, expected: dict[str, Any],
              operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        payload = {"reason": reason, "expected": dict(expected),
                   "operations": [dict(item) for item in (operations or [])],
                   "created_at": datetime.now(UTC).isoformat(timespec="seconds")}
        self.marker.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n",
                             encoding="utf-8")
        temporary.chmod(0o600)
        temporary.replace(self.marker)
        return self.status()

    def status(self) -> dict[str, Any]:
        if not self.marker.is_file():
            return {"paused": False, "reason": None, "expected": {},
                    "operations": [], "created_at": None}
        try:
            payload = json.loads(self.marker.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"paused": True, "reason": "restore_marker_unreadable",
                    "expected": {}, "operations": [], "created_at": None}
        if (not isinstance(payload, dict) or not isinstance(payload.get("expected"), dict)
                or not isinstance(payload.get("created_at"), str)
                or not isinstance(payload.get("operations", []), list)):
            # A marker that parses but does not carry the full fingerprint is
            # corrupt, not an empty expectation.  Treating it as {} would delete
            # it without checking anything.
            return {"paused": True, "reason": "restore_marker_invalid",
                    "expected": {}, "operations": [], "created_at": None}
        operations = [item for item in payload.get("operations", [])
                      if isinstance(item, dict)]
        return {"paused": True, "reason": str(payload.get("reason") or "restore_required"),
                "expected": dict(payload["expected"]), "operations": operations,
                "created_at": payload["created_at"]}

    def verify(self, current: dict[str, Any], *,
               operations: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        if not self.marker.is_file():
            return {"paused": False, "verified": False, "reason": "not_paused"}
        status = self.status()
        if status.get("reason") in {"restore_marker_unreadable", "restore_marker_invalid"}:
            raise RestoreError("restore_marker_unreadable",
                               mismatched=["restore_marker"])
        if current.get("error"):
            raise RestoreError("restore_environment_unavailable",
                               mismatched=[str(current["error"])])
        expected = status["expected"]
        mismatched = [key for key, value in expected.items()
                      if str(current.get(key)) != str(value)]
        expected_operations = status["operations"]
        if expected_operations:
            if operations is None:
                raise RestoreError("restore_operation_snapshot_unavailable",
                                   mismatched=["operations"])
            mismatched.extend(compare_operations(expected_operations, operations))
        if mismatched:
            raise RestoreError("restore_environment_mismatch", mismatched=mismatched)
        self.marker.unlink(missing_ok=True)
        return {"paused": False, "verified": True, "reason": "restore_verified",
                "operations_checked": len(expected_operations)}


def compare_operations(expected: list[dict[str, Any]],
                       current: list[dict[str, Any]]) -> list[str]:
    """Public form of the item-by-item operation comparison for drill scripts."""
    """Return one entry per missing/changed operation and per new unresolved write."""
    current_by_id = {str(item.get("effect_id")): item for item in current}
    expected_ids = {str(item.get("effect_id")) for item in expected}
    mismatched: list[str] = []
    for item in expected:
        effect_id = str(item.get("effect_id"))
        live = current_by_id.get(effect_id)
        if live is None:
            mismatched.append(f"operation:{effect_id}:missing")
            continue
        for field in ("ticket_id", "kind", "idempotency_key", "payload_sha256",
                      "side_effect_class"):
            if str(live.get(field)) != str(item.get(field)):
                mismatched.append(f"operation:{effect_id}:{field}")
    for item in current:
        effect_id = str(item.get("effect_id"))
        if effect_id in expected_ids:
            continue
        if str(item.get("state")) in UNRESOLVED_EFFECT_STATES and str(
                item.get("side_effect_class")) == "business_write":
            # A restored database that contains a business write nobody captured
            # is exactly the state that must not be reopened silently.
            mismatched.append(f"operation:{effect_id}:unexpected")
    return mismatched


def current_fingerprint(control_url: str | None = None) -> dict[str, Any]:
    url = control_url or os.environ.get("FORGEFLOW_CONTROL_DATABASE_URL", "")
    if not url:
        return {}
    connection = None
    try:
        connection = _connect(url)
        if connection is None:
            return {}
        with connection:
            database = connection.info.dbname
            row = connection.execute(
                "SELECT max(version) FROM control_schema_migrations").fetchone()
            last = connection.execute(
                "SELECT (SELECT id::text FROM control_effects "
                "ORDER BY updated_at DESC LIMIT 1) AS effect_id, "
                "(SELECT id::text FROM control_tickets "
                "ORDER BY updated_at DESC LIMIT 1) AS ticket_id").fetchone()
        fingerprint = {"control_db": database,
                       "migrations_max": int(row[0] or 0)}
        if last and last[0]:
            fingerprint["last_effect_id"] = str(last[0])
        if last and last[1]:
            fingerprint["last_ticket_id"] = str(last[1])
        return fingerprint
    except Exception:  # noqa: BLE001 - a configured but unreachable DB must fail closed
        return {"error": "control_database_unreachable"}
    finally:
        if connection is not None:
            connection.close()

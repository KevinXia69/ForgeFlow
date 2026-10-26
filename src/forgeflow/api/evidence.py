"""One authenticated read projection for the workbench and evidence MCP.

The MCP receives a host-generated snapshot, never raw stores or a model-chosen
identity. Building a snapshot has no execution/approval dependency.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..product import ProductCaseService
from ..product.service import actor_label
from ..runview import RUN_ID, build_run_view
from .runs import RunService
from .settings import Settings

PRIVATE_KEYS = {
    "uid", "approver", "nonce", "api_key", "token", "secret", "invite_code",
    "trial_uri", "workspace_root", "trials_dir", "session_secret",
    "executor_binding", "executor_grant", "owner_sha256", "_owner_sha256", "authorization_error",
    "preparation", "preparation_history", "_preparation", "_plan",
    "selection", "artifact_sha256", "comparison_sha256", "root_revision",
}


def public_evidence(value: Any) -> Any:
    if isinstance(value, dict):
        projected = {key: public_evidence(item) for key, item in value.items()
                     if key.lower() not in PRIVATE_KEYS}
        approver = value.get("approver")
        if isinstance(approver, str) and approver:
            # The authenticated service stores the principal. Publish the same
            # safe identity shown by auth/me, never its uid or approval nonce.
            projected["approver_label"] = actor_label(approver)
        elif "approver" in value:
            projected.pop("approver_label", None)
        return projected
    if isinstance(value, list):
        return [public_evidence(item) for item in value]
    return value


def principal_view(settings: Settings, uid: str) -> dict[str, str]:
    material = f"{settings.deployment}\0{settings.data_root.resolve()}\0{uid}"
    scope_id = hmac.new(settings.session_secret.encode(), material.encode(), hashlib.sha256).hexdigest()
    return {"scope_id": scope_id, "display_name": actor_label(uid), "deployment": settings.deployment}


def _order_key(record: dict[str, Any]) -> tuple[datetime, str]:
    try:
        stamp = datetime.fromisoformat(record.get("started_at"))
        stamp = stamp.replace(tzinfo=UTC) if stamp.tzinfo is None else stamp.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        stamp = datetime.min.replace(tzinfo=UTC)
    return stamp, str(record.get("run_id", ""))


class ReadOnlyEvidenceProjection:
    def __init__(self, settings: Settings, cases: ProductCaseService, runs: RunService, connection=None):
        self.settings = settings
        self.cases = cases
        self.runs = runs
        self.connection = connection

    def _case_view(self, case: dict[str, Any], connection: dict[str, Any] | None) -> dict[str, Any]:
        view = public_evidence(case)
        decision = view.get("decision")
        if not isinstance(decision, dict):
            return view
        if self.settings.deployment == "review":
            decision.update(can_approve=False, can_reject=False, can_request_changes=False,
                            can_reconcile=False, can_cancel_unstarted=False, can_resume_preparation=False,
                            can_resume_revision=False, can_reprepare=False, can_resume_repreparation=False,
                            can_select_candidate=False, can_resume_selection=False,
                            reason="review_read_only", revision_reason="review_read_only")
        elif connection is not None and connection.get("ready") is not True:
            if decision.get("can_approve"):
                decision.update(can_approve=False, reason="product_execution_unavailable")
            decision.update(can_request_changes=False, can_reconcile=False, can_cancel_unstarted=False,
                            can_resume_preparation=False, can_resume_revision=False,
                            can_reprepare=False, can_resume_repreparation=False,
                            can_select_candidate=False, can_resume_selection=False,
                            revision_reason="product_execution_unavailable")
        return view

    def list_cases(self, uid: str) -> list[dict[str, Any]]:
        connection = self.connection() if self.connection is not None else None
        return [self._case_view(case, connection) for case in self.cases.list_for(uid)]

    def get_case(self, case_id: str, uid: str) -> dict[str, Any] | None:
        case = self.cases.get(case_id, uid)
        connection = self.connection() if self.connection is not None and case is not None else None
        return self._case_view(case, connection) if case is not None else None

    def can_view_run(self, run_id: str, uid: str) -> bool:
        owner = self.runs.owner(run_id)
        if owner is not None:
            return owner == uid
        return "*" in self.settings.public_runs or run_id in self.settings.public_runs

    def get_run(self, run_id: str, uid: str) -> dict[str, Any] | None:
        if RUN_ID.fullmatch(run_id) is None or not self.can_view_run(run_id, uid):
            return None
        path = self.settings.data_root / run_id
        if path.is_symlink() or not path.is_dir():
            return None
        try:
            value = build_run_view(path, self.settings.data_root)
        except (OSError, ValueError, TypeError, OverflowError):
            return None
        if value is None:
            return None
        return public_evidence({**value, "access_scope": "owned" if self.runs.owner(run_id) == uid else "public_evidence"})

    def list_runs(self, uid: str) -> list[dict[str, Any]]:
        try:
            entries = sorted(self.settings.data_root.iterdir())
        except OSError:
            entries = []
        records = []
        for path in entries:
            view = self.get_run(path.name, uid)
            if view is None:
                continue
            records.append({
                "started_at": view["timing"]["started_at"],
                "run_id": view["run_id"], "task": view["task"]["id"],
                "task_label": view["task"]["label"],
                "score": view["evaluation"]["score"],
                "strict_success": view["evaluation"]["strict_success"],
                "evaluation_valid": view["evaluation"]["evaluation_valid"],
                "security_profile": view["experiment"]["security_profile"]["name"],
                "state": view["state"],
                "agent": view["agent"],
                "experiment": view["experiment"],
                "access_scope": view["access_scope"],
            })
        known = {record["run_id"] for record in records}
        for pending in self.runs.list_for(uid):
            if pending["run_id"] not in known:
                records.append({
                    "run_id": pending["run_id"], "task": pending.get("scenario"),
                    "task_label": pending.get("scenario"), "score": None,
                    "strict_success": None, "evaluation_valid": None,
                    "security_profile": "permission-split-v1",
                    "started_at": None,
                    "agent": {"name": "unknown", "category": "unclassified"},
                    "experiment": None,
                    "state": pending["state"], "access_scope": "owned",
                })
        records.sort(key=_order_key, reverse=True)
        return records

    def snapshot(self, uid: str) -> dict[str, Any]:
        runs = self.list_runs(uid)
        return {"schema_version": 1, "scope_id": principal_view(self.settings, uid)["scope_id"],
                "cases": self.list_cases(uid), "runs": runs,
                "run_details": [self.get_run(row["run_id"], uid) or row for row in runs]}

    def publish(self, uid: str) -> Path:
        """Atomically publish to a host-private path, outside the agent workspace."""
        value = self.snapshot(uid)
        shared_root = os.environ.get("FORGEFLOW_ASSISTANT_EVIDENCE_ROOT", "").strip()
        root = Path(shared_root) if shared_root else self.settings.approvals_root / "assistant-evidence"
        if shared_root and (not root.is_absolute() or not root.is_dir()):
            raise ValueError("invalid_evidence_store")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.is_symlink() or root.resolve() != root.absolute():
            raise ValueError("invalid_evidence_store")
        permissions = {
            "cases": sorted(case["case_id"] for case in value["cases"]),
            "runs": sorted((run["run_id"], run["access_scope"]) for run in value["runs"]),
        }
        revision = hashlib.sha256(json.dumps(permissions, sort_keys=True).encode()).hexdigest()[:16]
        path = root / f"{value['scope_id']}-{revision}.json"
        fd, temporary = tempfile.mkstemp(dir=root, prefix=".scope-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(value, handle, ensure_ascii=False, allow_nan=False)
                handle.flush()
                os.fsync(handle.fileno())
            if shared_root:
                os.chmod(temporary, 0o640)
            os.replace(temporary, path)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return path

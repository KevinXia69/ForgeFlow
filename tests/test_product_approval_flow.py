"""Service-level proof of token bindings and executor grant consumption.

These synthetic checks do not authorize historical HTTP plans. The business
API's rejection of that retired approval path is tested separately.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.approvals import ApprovalService
from forgeflow.api.settings import Settings, create_settings
from forgeflow.approval import Approval, ApprovalError, consume, used_digest
from forgeflow.executor import handlers
from forgeflow.executor.handlers import approval_wire_record as wire_approval
from forgeflow.executor.protocol import ProtocolError
from forgeflow.recovery import service

SECRET = "product-approval-flow-secret-0123456789"
INVITE = "product-approval-invite-0001"
RUN_ID = "product-approval-flow-run"
APPROVAL_FIELDS = handlers.APPROVAL_FIELDS


def _sha256(label: str) -> str:
    return hashlib.sha256(label.encode("ascii")).hexdigest()


POLICY_SHA256 = _sha256("synthetic-policy-v1")
SNAPSHOT_SHA256 = _sha256("synthetic-snapshot-v1")
PLAN_ID = _sha256("synthetic-plan-v1")


def write_plan(
    run_dir: Path,
    *,
    plan_id: str = PLAN_ID,
    policy: str | None = POLICY_SHA256,
) -> None:
    """Write a prepared plan in the product publish layout the API reads."""
    directory = run_dir / "plans" / plan_id
    directory.mkdir(parents=True)
    prepared: dict[str, Any] = {
        "ok": True,
        "odoo_writes": False,
        "plan_id": plan_id,
        "operation_counts": {"keep": 1, "update": 1, "cancel": 0, "create": 0},
        "step_count": 2,
        "expected_new_spend": 10.0,
    }
    if policy is not None:
        prepared["policy_sha256"] = policy
    (directory / "prepared.json").write_text(json.dumps(prepared), encoding="utf-8")
    (directory / "manifest.json").write_text(
        json.dumps({"schema_version": 1, "artifacts": {"snapshot": SNAPSHOT_SHA256}}),
        encoding="utf-8",
    )


def make_settings(tmp_path: Path, runs_root: Path) -> Settings:
    return create_settings(
        {
            "FORGEFLOW_DATA_ROOT": str(runs_root),
            "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
            "FORGEFLOW_SESSION_SECRET": SECRET,
            "FORGEFLOW_INVITE_CODES": INVITE,
            "FORGEFLOW_SECURE_COOKIE": "true",
            "FORGEFLOW_PUBLIC_RUNS": "*",
        }
    )


def sign_in(client: TestClient) -> str:
    response = client.post("/api/auth/session", json={"invite_code": INVITE})
    assert response.status_code == 201, response.text
    return response.json()["uid"]


def approval_from(record: dict[str, Any]) -> Approval:
    """Build the executor's value object from the product response fields."""
    return Approval(**{name: record[name] for name in APPROVAL_FIELDS})



def claim(
    store: Path,
    approval: Approval,
    *,
    now: datetime | None = None,
    **overrides: str,
) -> dict[str, Any]:
    """Call the real consume() with the record's own bindings, or overrides."""
    binding: dict[str, Any] = {
        "run_id": approval.run_id,
        "plan_id": approval.plan_id,
        "policy_sha256": approval.policy_sha256,
        "snapshot_sha256": approval.snapshot_sha256,
        "approver": approval.approver,
        "now": now,
    }
    binding.update(overrides)
    return consume(store, approval, **binding)


@dataclass(frozen=True)
class Harness:
    client: TestClient
    settings: Settings
    uid: str
    run_id: str
    plan_id: str

    @property
    def store(self) -> Path:
        """The exact directory the API writes: approvals_root/run_id/plan_id."""
        assert self.settings.approvals_root is not None
        return self.settings.approvals_root / self.run_id / self.plan_id

    def approve(self, *, ttl_seconds: int = 900) -> dict[str, Any]:
        return ApprovalService(self.settings.approvals_root).issue(
            run_id=self.run_id, plan_id=self.plan_id, approver=self.uid,
            policy_sha256=POLICY_SHA256, snapshot_sha256=SNAPSHOT_SHA256,
            ttl_seconds=ttl_seconds,
        )


@pytest.fixture
def harness(tmp_path: Path) -> Iterator[Harness]:
    runs_root = tmp_path / "runs"
    runs_root.mkdir()
    write_plan(runs_root / RUN_ID)
    settings = make_settings(tmp_path, runs_root)
    with TestClient(create_app(settings), base_url="https://testserver") as client:
        uid = sign_in(client)
        yield Harness(
            client=client,
            settings=settings,
            uid=uid,
            run_id=RUN_ID,
            plan_id=PLAN_ID,
        )


def make_handler(tmp_path: Path, approval_store: Path) -> handlers.RecoveryHandlers:
    store = tmp_path / "executor" / "recovery"
    store.mkdir(parents=True)
    return handlers.RecoveryHandlers(
        connection_factory=lambda: "synthetic-connection",
        store=store,
        bundle=tmp_path / "bundle",
        approval_store=approval_store,
        require_grant=True,
    )


def test_service_approval_record_drives_executor_apply(
    harness: Harness, tmp_path: Path, monkeypatch
) -> None:
    record = harness.approve()

    assert set(record) == set(APPROVAL_FIELDS) | {"schema_version", "status"}
    assert record["status"] == "valid"
    approval = approval_from(record)
    token = harness.store / f"approval-{approval.nonce}.json"
    assert json.loads(token.read_text(encoding="utf-8")) == approval.payload()
    assert used_digest(harness.store, approval) is None

    handler = make_handler(tmp_path, harness.store)
    seen: dict[str, Any] = {}

    def recorder(connection, plan_id, *, store, bundle, grant, require_grant, approval_store):
        assert plan_id == harness.plan_id
        assert connection == "synthetic-connection"
        assert Path(store) == handler.store
        assert Path(bundle) == handler.bundle
        assert Path(approval_store) == harness.store
        assert require_grant is True
        assert grant is not None
        # The grant the real service would consume carries the API record.
        for name in APPROVAL_FIELDS:
            assert getattr(grant.approval, name) == record[name]
        assert {
            name: getattr(grant.approval, name) for name in APPROVAL_FIELDS
        } == wire_approval(record)
        assert grant.run_id == record["run_id"] == harness.run_id
        assert grant.policy_sha256 == record["policy_sha256"] == POLICY_SHA256
        assert grant.snapshot_sha256 == record["snapshot_sha256"] == SNAPSHOT_SHA256
        assert grant.approver == record["approver"] == harness.uid
        seen["called"] = True
        return {"ok": True, "plan_id": plan_id, "execution_validated": True}

    monkeypatch.setattr(service, "apply", recorder)
    result = handler.apply(harness.run_id, harness.plan_id, wire_approval(record))

    assert seen == {"called": True}
    assert result == {
        "ok": True,
        "plan_id": harness.plan_id,
        "execution_validated": True,
        "run_id": harness.run_id,
    }
    # The handler builds the grant but does not spend it; service.apply would.
    assert used_digest(harness.store, approval) is None


def test_service_record_is_consumed_exactly_once(harness: Harness) -> None:
    record = harness.approve()
    approval = approval_from(record)
    assert used_digest(harness.store, approval) is None

    assert claim(harness.store, approval) == approval.payload()
    assert used_digest(harness.store, approval) == approval.digest()

    with pytest.raises(ApprovalError) as error:
        claim(harness.store, approval)
    assert str(error.value) == "approval_already_consumed"
    assert used_digest(harness.store, approval) == approval.digest()

    # The product lists the same store, so it observes the executor's marker.
    listed = harness.client.get(
        f"/api/runs/{harness.run_id}/plans/{harness.plan_id}/approvals"
    )
    assert listed.status_code == 200
    assert [item["status"] for item in listed.json()] == ["used"]


@pytest.mark.parametrize(
    ("field", "value", "expected"),
    [
        ("run_id", "tampered-run", "approval_run_mismatch"),
        ("policy_sha256", "f" * 64, "approval_policy_mismatch"),
        ("snapshot_sha256", "e" * 64, "approval_snapshot_mismatch"),
    ],
)
def test_consume_rejects_a_tampered_binding(
    harness: Harness, field: str, value: str, expected: str
) -> None:
    record = harness.approve()
    approval = approval_from(record)

    with pytest.raises(ApprovalError) as error:
        claim(harness.store, approval, **{field: value})
    assert str(error.value) == expected
    # A rejected call must not spend a still-valid record.
    assert used_digest(harness.store, approval) is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_id", "tampered-run"),
        ("policy_sha256", "f" * 64),
        ("snapshot_sha256", "e" * 64),
    ],
)
def test_a_tampered_copy_of_the_record_is_not_the_stored_token(
    harness: Harness, field: str, value: str
) -> None:
    record = harness.approve()
    tampered = dict(record)
    tampered[field] = value
    approval = approval_from(tampered)

    with pytest.raises(ApprovalError) as error:
        claim(harness.store, approval)
    assert str(error.value) == "approval_token_tampered"
    assert used_digest(harness.store, approval) is None


def test_api_refuses_a_plan_without_a_policy(harness: Harness) -> None:
    plan_id = _sha256("synthetic-plan-without-policy")
    write_plan(
        harness.settings.data_root / "no-policy-run",
        plan_id=plan_id,
        policy=None,
    )
    detail = harness.client.get(f"/api/runs/no-policy-run/plans/{plan_id}")
    assert detail.status_code == 200
    assert detail.json()["policy_sha256"] is None

    response = harness.client.post(
        f"/api/runs/no-policy-run/plans/{plan_id}/approvals",
        json={"ttl_seconds": 900},
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "historical_evidence_read_only"
    assert harness.settings.approvals_root is not None
    assert not (harness.settings.approvals_root / "no-policy-run" / plan_id).exists()


def test_expiry_is_rejected_by_consume_and_by_the_handler(
    harness: Harness, tmp_path: Path, monkeypatch
) -> None:
    record = harness.approve(ttl_seconds=60)
    approval = approval_from(record)
    expires_at = datetime.fromisoformat(record["expires_at_utc"])
    assert expires_at.tzinfo is not None

    with pytest.raises(ApprovalError) as error:
        claim(harness.store, approval, now=expires_at + timedelta(seconds=1))
    assert str(error.value) == "approval_expired"
    assert used_digest(harness.store, approval) is None

    handler = make_handler(tmp_path, harness.store)
    seen: dict[str, Any] = {}

    def expired(connection, plan_id, *, grant, **kwargs):
        assert grant is not None
        assert grant.approval.payload() == approval.payload()
        seen["grant"] = grant
        raise service.RecoveryError("approval_expired")

    monkeypatch.setattr(service, "apply", expired)
    with pytest.raises(ProtocolError) as handler_error:
        handler.apply(harness.run_id, harness.plan_id, wire_approval(record))

    assert seen["grant"].approval.nonce == record["nonce"]
    assert handler_error.value.code == "approval_rejected"
    assert handler_error.value.status == 403
    assert handler_error.value.reason == "approval_expired"
    assert used_digest(harness.store, approval) is None
    logged = [
        json.loads(line)
        for line in (handler.store.parent / "executor-errors.jsonl").read_text().splitlines()
    ]
    assert logged == [{"op": "apply", "reason": "approval_expired"}]

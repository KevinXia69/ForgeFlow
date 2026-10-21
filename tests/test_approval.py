from __future__ import annotations

import json
import os
import stat
from datetime import UTC, datetime, timedelta

import pytest

from forgeflow.approval import Approval, ApprovalError, consume, issue, used_digest

PLAN = "a" * 64
POLICY = "b" * 64
SNAPSHOT = "c" * 64
NOW = datetime(2026, 10, 21, 12, 0, tzinfo=UTC)


def make(tmp_path, **overrides):
    values = dict(
        run_id="run-1", plan_id=PLAN, policy_sha256=POLICY, snapshot_sha256=SNAPSHOT,
        approver="judge@example.invalid", ttl_seconds=900, now=NOW,
    )
    values.update(overrides)
    return issue(tmp_path, **values)


def spend(tmp_path, approval, **overrides):
    values = dict(
        run_id="run-1", plan_id=PLAN, policy_sha256=POLICY, snapshot_sha256=SNAPSHOT,
        approver="judge@example.invalid", now=NOW + timedelta(seconds=1),
    )
    values.update(overrides)
    return consume(tmp_path, approval, **values)


def test_issue_then_consume_once(tmp_path):
    approval = make(tmp_path)
    payload = spend(tmp_path, approval)
    assert payload["plan_id"] == PLAN
    assert (tmp_path / f"approval-{approval.nonce}.used").is_file()


def test_replay_is_rejected(tmp_path):
    approval = make(tmp_path)
    spend(tmp_path, approval)
    with pytest.raises(ApprovalError, match="approval_already_consumed"):
        spend(tmp_path, approval)


def test_expired_approval_is_rejected(tmp_path):
    approval = make(tmp_path, ttl_seconds=30)
    with pytest.raises(ApprovalError, match="approval_expired"):
        spend(tmp_path, approval, now=NOW + timedelta(seconds=30))
    assert not (tmp_path / f"approval-{approval.nonce}.used").exists()


def test_expiry_boundary_is_strict(tmp_path):
    approval = make(tmp_path, ttl_seconds=60)
    spend(tmp_path, approval, now=NOW + timedelta(seconds=59))


@pytest.mark.parametrize("field,value,code", [
    ("run_id", "run-2", "approval_run_mismatch"),
    ("plan_id", "d" * 64, "approval_plan_mismatch"),
    ("policy_sha256", "e" * 64, "approval_policy_mismatch"),
    ("snapshot_sha256", "f" * 64, "approval_snapshot_mismatch"),
    ("approver", "someone@example.invalid", "approval_approver_mismatch"),
])
def test_binding_mismatch_is_rejected_without_burning_the_token(tmp_path, field, value, code):
    approval = make(tmp_path)
    with pytest.raises(ApprovalError, match=code):
        spend(tmp_path, approval, **{field: value})
    assert not (tmp_path / f"approval-{approval.nonce}.used").exists()
    spend(tmp_path, approval)


def test_tampered_token_file_is_rejected(tmp_path):
    approval = make(tmp_path)
    path = tmp_path / f"approval-{approval.nonce}.json"
    payload = json.loads(path.read_text())
    payload["approver"] = "attacker@example.invalid"
    path.write_text(json.dumps(payload))
    with pytest.raises(ApprovalError, match="approval_token_tampered"):
        spend(tmp_path, approval)


def test_missing_token_is_rejected(tmp_path):
    approval = make(tmp_path)
    (tmp_path / f"approval-{approval.nonce}.json").unlink()
    with pytest.raises(ApprovalError, match="approval_not_found"):
        spend(tmp_path, approval)


def test_token_file_is_private(tmp_path):
    approval = make(tmp_path)
    mode = stat.S_IMODE((tmp_path / f"approval-{approval.nonce}.json").stat().st_mode)
    assert mode == 0o600


def test_digest_is_stable_and_order_independent():
    approval = Approval(
        nonce="0" * 32, run_id="r", plan_id=PLAN, policy_sha256=POLICY,
        snapshot_sha256=SNAPSHOT, approver="a", issued_at_utc="t1", expires_at_utc="t2",
    )
    assert approval.digest() == approval.digest()
    assert len(approval.digest()) == 64


@pytest.mark.parametrize("overrides,code", [
    ({"ttl_seconds": 0}, "invalid_ttl"),
    ({"ttl_seconds": 24 * 60 * 60 + 1}, "invalid_ttl"),
    ({"plan_id": "short"}, "invalid_plan_id"),
    ({"policy_sha256": "x"}, "invalid_policy_sha256"),
    ({"run_id": "../escape"}, "invalid_run_id"),
    ({"approver": "bad approver"}, "invalid_approver"),
])
def test_issue_validates_inputs(tmp_path, overrides, code):
    with pytest.raises(ApprovalError, match=code):
        make(tmp_path, **overrides)


def test_naive_clock_is_rejected(tmp_path):
    with pytest.raises(ApprovalError, match="approval_clock_requires_timezone"):
        make(tmp_path, now=datetime(2026, 10, 21, 12, 0))


def test_marker_records_the_digest_and_leaves_no_temporary_files(tmp_path):
    approval = make(tmp_path)
    assert used_digest(tmp_path, approval) is None
    spend(tmp_path, approval)
    assert used_digest(tmp_path, approval) == approval.digest()
    leftovers = [path.name for path in tmp_path.iterdir() if path.suffix == ".tmp"]
    assert leftovers == []


def test_an_empty_or_foreign_marker_does_not_authorize_resume(tmp_path):
    approval = make(tmp_path)
    (tmp_path / f"approval-{approval.nonce}.used").write_text("")
    assert used_digest(tmp_path, approval) is None
    (tmp_path / f"approval-{approval.nonce}.used").write_text("f" * 64)
    assert used_digest(tmp_path, approval) != approval.digest()


def test_a_second_unexpired_approval_for_the_same_plan_is_rejected(tmp_path):
    make(tmp_path)
    with pytest.raises(ApprovalError, match="approval_exists"):
        make(tmp_path)


def test_a_failed_marker_write_leaves_no_temp_file_and_can_be_retried(tmp_path, monkeypatch):
    approval = make(tmp_path)
    real_write = os.write
    calls = {"n": 0}

    def flaky(fd, data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk full")
        return real_write(fd, data)

    monkeypatch.setattr(os, "write", flaky)
    with pytest.raises(ApprovalError, match="marker_write_failed"):
        spend(tmp_path, approval)
    monkeypatch.setattr(os, "write", real_write)
    assert used_digest(tmp_path, approval) is None
    assert not [path for path in tmp_path.iterdir() if path.suffix == ".tmp"]
    spend(tmp_path, approval)

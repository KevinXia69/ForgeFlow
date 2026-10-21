"""Durable service orchestration with invented ERP state and simulated writes."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
import tarfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from recovery_synthetic import Fake, inputs

from forgeflow.approval import Approval, issue
from forgeflow.contract import build_contract
from forgeflow.planning.bundle import build_planner_bundle
from forgeflow.recovery import scope_guard, service
from forgeflow.recovery.journal import ExecutionStopped
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.service import ExecutionGrant
from forgeflow.recovery.storage import StorageError, atomic_json, read_json, scope_lock


@pytest.fixture
def system(monkeypatch, tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "bundle.tar.gz"
    record = build_planner_bundle(root, archive, recovery=True)
    with tarfile.open(archive) as handle:
        handle.extractall(tmp_path, filter="data")
    request, snapshot, blueprint, plan = inputs()
    connection = Fake(snapshot)
    connection.writes = []
    connection.fail = None
    monkeypatch.setattr(service, "collect_snapshot", lambda client, request: copy.deepcopy(snapshot))
    monkeypatch.setattr(scope_guard, "collect_snapshot", lambda client, request: client.collect(request))
    monkeypatch.setattr(service, "solve_blueprint", lambda given, upstream: copy.deepcopy(plan))
    original_apply = ReadbackAdapter.apply

    def fake_apply(adapter, step):
        if step.kind == "bind_workorder":
            return original_apply(adapter, step)
        connection.writes.append(step.key)
        if connection.fail == "before":
            raise TimeoutError("private response")
        connection.apply(adapter, step)
        if connection.fail == "after":
            connection.fail = None
            raise TimeoutError("private response")

    monkeypatch.setattr(ReadbackAdapter, "apply", fake_apply)
    options = {"store": tmp_path / "state", "bundle": tmp_path / "planner"}
    return connection, request, options, record


def test_prepare_persists_originals_then_apply_and_replay_without_duplicate_writes(system):
    connection, request, options, record = system
    before = copy.deepcopy(connection.db)
    prepared = service.prepare(connection, request, **options)
    assert prepared["odoo_writes"] is False and connection.db == before
    directory = options["store"] / prepared["plan_id"]
    assert read_json(directory / "manifest.json")["sources"] == record
    baseline = (directory / "baseline.json").read_bytes()
    result = service.apply(connection, prepared["plan_id"], **options)
    assert result["execution_validated"] is True and result["benchmark_success"] is None
    assert connection.writes
    original_calls = list(connection.writes)
    assert service.apply(connection, prepared["plan_id"], **options) == result
    assert connection.writes == original_calls
    assert (directory / "baseline.json").read_bytes() == baseline


@pytest.mark.parametrize("artifact", ["baseline", "snapshot", "change-set", "binding"])
def test_changed_original_artifact_stops_before_any_write(system, artifact):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    path = options["store"] / prepared["plan_id"] / (artifact + ".json")
    value = read_json(path)
    value["tampered"] = True
    atomic_json(path, value, replace=True)
    with pytest.raises(service.RecoveryError, match="original_plan_artifact_changed"):
        service.apply(connection, prepared["plan_id"], **options)
    assert not connection.writes


def test_source_change_cannot_reuse_old_baseline_even_with_identical_steps(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    source = options["bundle"] / "forgeflow/recovery/odoo.py"
    source.chmod(0o600)
    source.write_bytes(source.read_bytes() + b"\n# changed\n")
    with pytest.raises(service.RecoveryError, match="source_bytes_changed"):
        service.apply(connection, prepared["plan_id"], **options)
    assert not connection.writes


def test_committed_response_loss_is_read_back_without_repeating_mutation(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    connection.fail = "after"
    result = service.apply(connection, prepared["plan_id"], **options)
    assert result["execution_validated"] is True
    assert len(connection.writes) == len(set(connection.writes))


def test_unknown_outcome_blocks_replanning_and_blind_dispatch(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    connection.fail = "before"
    with pytest.raises(ExecutionStopped):
        service.apply(connection, prepared["plan_id"], **options)
    original_calls = list(connection.writes)
    with pytest.raises(service.RecoveryError, match="unfinished_execution_requires_original_plan"):
        service.prepare(connection, request, **options)
    with pytest.raises(ExecutionStopped):
        service.apply(connection, prepared["plan_id"], **options)
    assert connection.writes == original_calls


def test_scope_lock_prevents_second_plan_execution(system):
    connection, request, options, _ = system
    with scope_lock(options["store"]):
        with pytest.raises(StorageError, match="another_recovery_operation_running"):
            service.prepare(connection, request, **options)
    assert not connection.writes


def test_zero_dispatch_stale_preflight_does_not_hold_scope_forever(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    connection.db['sale.order'][1]['commitment_date'] = '2026-02-01 00:00:00'
    with pytest.raises(ExecutionStopped, match='preflight_failed'):
        service.apply(connection, prepared['plan_id'], **options)
    assert not connection.writes
    assert not (options['store'] / 'active-plan.json').exists()
    assert (options['store'] / prepared['plan_id'] / 'baseline.json').is_file()


def test_exclusive_artifacts_and_symlink_reads_cannot_replace_baseline(tmp_path):
    path = tmp_path / "original.json"
    atomic_json(path, {"original": True})
    with pytest.raises(FileExistsError):
        atomic_json(path, {"original": False})
    assert read_json(path) == {"original": True}
    link = tmp_path / "link.json"
    link.symlink_to(path)
    with pytest.raises(OSError):
        read_json(link)


@pytest.mark.parametrize("value", ["../other", "A" * 64, "a" * 63, "a" * 64 + "\n", None])
def test_apply_rejects_unbound_paths_before_filesystem_or_rpc(system, value):
    connection, _, options, _ = system
    with pytest.raises(service.RecoveryError, match="invalid_plan_id"):
        service.apply(connection, value, **options)
    assert not connection.writes and not options["store"].exists()


def _grant(store, run_id, plan_id, snapshot_sha256, *, ttl=900, now=None, approver="judge"):
    approval = issue(
        store, run_id=run_id, plan_id=plan_id, policy_sha256="a" * 64,
        snapshot_sha256=snapshot_sha256, approver=approver, ttl_seconds=ttl, now=now,
    )
    return ExecutionGrant(
        approval=approval, run_id=run_id, policy_sha256="a" * 64,
        snapshot_sha256=snapshot_sha256, approver=approver,
    )


def test_grant_bound_to_another_snapshot_stops_before_any_write(system, tmp_path):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    approvals = tmp_path / "approvals"
    grant = _grant(approvals, "run-1", prepared["plan_id"], "b" * 64)
    with pytest.raises(service.RecoveryError, match="approval_snapshot_mismatch"):
        service.apply(
            connection, prepared["plan_id"], grant=grant,
            approval_store=approvals, **options,
        )
    assert connection.writes == []


def test_grant_is_consumed_on_first_dispatch_and_replay_needs_no_token(system, tmp_path):
    connection, request, options, _ = system
    contract = _contract()
    prepared = service.prepare(connection, request, contract=contract, **options)
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approvals = tmp_path / "approvals"
    grant = ExecutionGrant(
        approval=issue(
            approvals, run_id="run-1", plan_id=prepared["plan_id"],
            policy_sha256=contract.digest(),
            snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
        ),
        run_id="run-1", policy_sha256=contract.digest(),
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
    )
    result = service.apply(
        connection, prepared["plan_id"], grant=grant,
        approval_store=approvals, **options,
    )
    assert result["execution_validated"] is True
    assert (approvals / f"approval-{grant.approval.nonce}.used").is_file()
    writes = list(connection.writes)
    assert service.apply(
        connection, prepared["plan_id"], grant=grant,
        approval_store=approvals, **options,
    ) == result
    assert connection.writes == writes


def test_expired_grant_stops_before_dispatch_and_releases_the_plan_marker(system, tmp_path):
    connection, request, options, _ = system
    contract = _contract()
    prepared = service.prepare(connection, request, contract=contract, **options)
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approvals = tmp_path / "approvals"
    issued_at = datetime(2026, 1, 1, tzinfo=UTC)
    grant = ExecutionGrant(
        approval=issue(
            approvals, run_id="run-1", plan_id=prepared["plan_id"],
            policy_sha256=contract.digest(),
            snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
            ttl_seconds=60, now=issued_at,
        ),
        run_id="run-1", policy_sha256=contract.digest(),
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
    )
    with pytest.raises(service.RecoveryError, match="approval_expired"):
        service.apply(
            connection, prepared["plan_id"], grant=grant, approval_store=approvals,
            now=datetime(2026, 1, 1, 0, 5, tzinfo=UTC), **options,
        )
    assert connection.writes == []
    assert not (options["store"] / "active-plan.json").exists()


def test_grant_for_another_run_is_rejected_before_any_write(system, tmp_path):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approvals = tmp_path / "approvals"
    grant = _grant(approvals, "run-1", prepared["plan_id"],
                   manifest["artifacts"]["snapshot"])
    mismatched = ExecutionGrant(
        approval=grant.approval, run_id="run-2", policy_sha256=grant.policy_sha256,
        snapshot_sha256=grant.snapshot_sha256, approver=grant.approver,
    )
    with pytest.raises(service.RecoveryError, match="approval_run_mismatch"):
        service.apply(
            connection, prepared["plan_id"], grant=mismatched,
            approval_store=approvals, **options,
        )
    assert connection.writes == []


def _contract(**overrides):
    values = dict(
        contract_id="synthetic-outage-v1",
        task_id="synthetic_task",
        task_type="workcenter_outage",
        product_code="FIN",
        anchor_date="2026-01-01",
        margin=0.2,
        margin_basis="portfolio_new_spend_margin_at_selling_price",
        budgets={"1": 2000.0},
        sales_order_ids=(1,),
        disruption={
            "kind": "workcenter_outage",
            "workcenter_code": "WC-A",
            "workcenter_name": "Assembly A",
        },
        forbidden_workcenters=(),
        forbidden_operation_kinds=(),
        original_commitment_policy="keep_commitments_that_still_work",
        sources={"budgets": "synthetic"},
    )
    values.update(overrides)
    return build_contract(**values)


def test_prepare_rejects_a_request_that_conflicts_with_the_contract(system):
    connection, request, options, _ = system
    contract = _contract(margin=0.25)
    with pytest.raises(service.RecoveryError, match="task_contract_mismatch"):
        service.prepare(connection, request, contract=contract, **options)
    assert not (options["store"] / "active-plan.json").exists()


def test_prepare_rejects_a_change_set_that_violates_the_contract(system):
    connection, request, options, _ = system
    contract = _contract(forbidden_operation_kinds=("purchase_order",))
    with pytest.raises(service.RecoveryError, match="plan_contract_violation"):
        service.prepare(connection, request, contract=contract, **options)


def test_policy_file_binds_the_grant_to_the_approved_contract(system, tmp_path):
    connection, request, options, _ = system
    contract = _contract()
    prepared = service.prepare(connection, request, contract=contract, **options)
    policy = options["store"].parent / "policy.json"
    assert policy.is_file()
    assert read_json(options["store"] / prepared["plan_id"] / "prepared.json")[
        "policy_sha256"] == contract.digest()
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approvals = tmp_path / "approvals"
    approval = issue(
        approvals, run_id="run-1", plan_id=prepared["plan_id"],
        policy_sha256=contract.digest(),
        snapshot_sha256=manifest["artifacts"]["snapshot"],
        approver="judge",
    )
    grant = ExecutionGrant(
        approval=approval, run_id="run-1", policy_sha256=contract.digest(),
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
    )
    result = service.apply(
        connection, prepared["plan_id"], grant=grant, approval_store=approvals, **options,
    )
    assert result["execution_validated"] is True


def test_grant_with_a_different_policy_is_rejected(system, tmp_path):
    connection, request, options, _ = system
    contract = _contract()
    prepared = service.prepare(connection, request, contract=contract, **options)
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approvals = tmp_path / "approvals"
    wrong_policy = "f" * 64
    approval = issue(
        approvals, run_id="run-1", plan_id=prepared["plan_id"],
        policy_sha256=wrong_policy,
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
    )
    grant = ExecutionGrant(
        approval=approval, run_id="run-1", policy_sha256=wrong_policy,
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
    )
    with pytest.raises(service.RecoveryError, match="approval_policy_mismatch"):
        service.apply(
            connection, prepared["plan_id"], grant=grant,
            approval_store=approvals, **options,
        )
    assert connection.writes == []


def test_require_grant_refuses_a_bare_plan_id(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    with pytest.raises(service.RecoveryError, match="approval_required"):
        service.apply(connection, prepared["plan_id"], require_grant=True, **options)
    assert connection.writes == []


def test_policy_file_without_a_recorded_digest_is_rejected(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    contract = _contract()
    atomic_json(options["store"].parent / "policy.json", contract.payload(), replace=True)
    with pytest.raises(service.RecoveryError, match="approval_policy_mismatch"):
        service.apply(connection, prepared["plan_id"], **options)
    assert connection.writes == []


def test_symlinked_policy_file_is_rejected(system, tmp_path):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    target = tmp_path / "elsewhere.json"
    atomic_json(target, _contract().payload(), replace=True)
    (options["store"].parent / "policy.json").symlink_to(target)
    with pytest.raises(service.RecoveryError, match="policy_file_untrusted"):
        service.apply(connection, prepared["plan_id"], **options)
    assert connection.writes == []


def test_require_grant_demands_a_recorded_policy_hash(system, tmp_path):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, **options)
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approvals = tmp_path / "approvals"
    grant = _grant(approvals, "run-1", prepared["plan_id"],
                   manifest["artifacts"]["snapshot"])
    with pytest.raises(service.RecoveryError, match="approval_policy_mismatch"):
        service.apply(
            connection, prepared["plan_id"], grant=grant, require_grant=True,
            approval_store=approvals, **options,
        )
    assert connection.writes == []


def test_recovery_bundle_packages_the_gate_modules_and_imports(tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "bundle.tar.gz"
    record = build_planner_bundle(root, archive, recovery=True)
    assert "forgeflow/approval.py" in record["files_sha256"]
    assert "forgeflow/contract.py" in record["files_sha256"]
    assert "forgeflow/executor/protocol.py" in record["files_sha256"]
    assert "forgeflow/recovery/index.py" in record["files_sha256"]
    extracted = tmp_path / "bundle"
    with tarfile.open(archive) as handle:
        handle.extractall(extracted, filter="data")
    imported = subprocess.run(
        [sys.executable, "-c",
         "import forgeflow.recovery.service, forgeflow.approval, forgeflow.contract, "
         "forgeflow.recovery.client, forgeflow.recovery.index, forgeflow.executor.protocol, "
         "forgeflow.executor.peer, forgeflow.executor.bootstrap, "
         "forgeflow.executor.daemon, forgeflow.executor.handlers, "
         "forgeflow.executor.main, forgeflow.odoo_credentials"],
        cwd=extracted, env={"PYTHONPATH": str(extracted)}, capture_output=True, text=True,
    )
    assert imported.returncode == 0, imported.stderr


def test_cli_apply_requires_an_explicit_autonomous_flag(capsys):
    code = service.main(["apply", "--plan-id", "a" * 64])
    assert code == 2
    output = json.loads(capsys.readouterr().out)
    assert output["error_code"] == "approval_required"
    assert output["odoo_writes"] is False


def test_grant_with_an_unreadable_expiry_is_rejected(system, tmp_path):
    connection, request, options, _ = system
    contract = _contract()
    prepared = service.prepare(connection, request, contract=contract, **options)
    manifest = read_json(options["store"] / prepared["plan_id"] / "manifest.json")
    approval = Approval(
        nonce="0" * 32, run_id="run-1", plan_id=prepared["plan_id"],
        policy_sha256=contract.digest(),
        snapshot_sha256=manifest["artifacts"]["snapshot"],
        approver="judge", issued_at_utc="2026-01-01T00:00:00+00:00",
        expires_at_utc="not-a-date",
    )
    grant = ExecutionGrant(
        approval=approval, run_id="run-1", policy_sha256=contract.digest(),
        snapshot_sha256=manifest["artifacts"]["snapshot"], approver="judge",
    )
    with pytest.raises(service.RecoveryError, match="invalid_execution_grant"):
        service.apply(connection, prepared["plan_id"], grant=grant,
                      approval_store=tmp_path / "approvals", **options)
    assert connection.writes == []

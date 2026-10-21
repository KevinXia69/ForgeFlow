import copy

import pytest
from test_recovery_service import _contract, system  # noqa: F401

from forgeflow.approval import issue
from forgeflow.product import reconciliation as module
from forgeflow.recovery import scope_guard, service
from forgeflow.recovery.service import ExecutionGrant
from forgeflow.recovery.storage import StorageError, atomic_json, digest, read_json, scope_lock

RUN = "local-" + "1" * 32
PASS = {"status": "passed", "strict_success": True, "evaluation_valid": True,
        "evaluation_scope": "product_original_verifier"}


@pytest.fixture
def completed(request, monkeypatch):
    connection, request, options, _ = request.getfixturevalue("system")
    contract = _contract()
    prepared = service.prepare(connection, request, contract=contract, **options)
    plan_id = prepared["plan_id"]
    manifest = read_json(options["store"] / plan_id / "manifest.json")
    approvals = options["store"].parent / "tokens"
    token = issue(approvals, run_id=RUN, plan_id=plan_id, policy_sha256=contract.digest(),
                  snapshot_sha256=manifest["artifacts"]["snapshot"], approver="operator")
    grant = ExecutionGrant(token, RUN, token.policy_sha256, token.snapshot_sha256, token.approver)
    service.apply(connection, plan_id, grant=grant, approval_store=approvals, require_grant=True, **options)
    monkeypatch.setattr(scope_guard, "collect_snapshot", lambda client, request: connection.collect(request))
    return connection, plan_id, dict(options, approvals=approvals), token


def test_recover_missing_verifier_under_lock_without_write_or_new_token(completed):
    connection, plan, options, token = completed
    db, writes = copy.deepcopy(connection.db), connection.writes[:]
    directory = options["store"] / plan
    journal = (directory / "journal.jsonl").read_bytes()
    tokens = sorted(p.name for p in options["approvals"].iterdir())
    calls = []

    def verifier():
        calls.append(True)
        with pytest.raises(StorageError, match="another_recovery_operation_running"):
            with scope_lock(options["store"]):
                pass
        return PASS

    result = module.reconcile_original(connection, RUN, plan, **options, verifier=verifier)
    assert result["execution_validated"] is True
    assert result["verifier"] == PASS
    assert result["policy_sha256"] == token.policy_sha256
    assert result["snapshot_sha256"] == token.snapshot_sha256
    assert result["case_id"] == "1" * 32
    again = module.reconcile_original(connection, RUN, plan, **options, verifier=verifier)
    assert again == result and len(calls) == 1
    assert connection.db == db and connection.writes == writes
    assert (directory / "journal.jsonl").read_bytes() == journal
    assert sorted(p.name for p in options["approvals"].iterdir()) == tokens


@pytest.mark.parametrize("fault", ["wrong_run", "missing_token", "partial", "corrupt", "artifact", "drift", "other_plan"])
def test_missing_partial_wrong_run_or_drift_never_starts_verifier(completed, fault):
    connection, plan, options, token = completed
    directory = options["store"] / plan
    run = RUN
    if fault == "wrong_run":
        run = "local-" + "2" * 32
    elif fault == "missing_token":
        (options["approvals"] / f"approval-{token.nonce}.used").unlink()
    elif fault == "partial":
        path = directory / "journal.jsonl"
        path.write_bytes(b"\n".join(path.read_bytes().splitlines()[:-1]) + b"\n")
    elif fault == "corrupt":
        with (directory / "journal.jsonl").open("ab") as out:
            out.write(b"bad")
    elif fault == "artifact":
        (directory / "snapshot.json").write_text("{}")
    elif fault == "drift":
        connection.db["sale.order"][1]["commitment_date"] = "2026-02-01 00:00:00"
    else:
        (options["store"] / "active-plan.json").write_text('{"plan_id":"' + "a" * 64 + '"}')
    before, writes = copy.deepcopy(connection.db), connection.writes[:]
    with pytest.raises(Exception):
        module.reconcile_original(connection, run, plan, **options,
                                  verifier=lambda: pytest.fail("must not run verifier"))
    assert connection.db == before and connection.writes == writes
    assert not (directory / "product-verification.json").exists()


def test_original_failed_result_is_cached_and_cannot_be_retried_to_pass(completed):
    connection, plan, options, _ = completed
    failed = dict(PASS, status="failed", strict_success=False)
    first = module.reconcile_original(connection, RUN, plan, **options, verifier=lambda: failed)
    second = module.reconcile_original(connection, RUN, plan, **options,
                                       verifier=lambda: pytest.fail("do not rerun failure"))
    assert first == second and first["verifier"]["strict_success"] is False


def test_raw_original_reward_is_saved_privately_but_not_returned_over_rpc(completed):
    import hashlib
    import json

    connection, plan, options, _ = completed
    raw = '{"passed":true,"rules":{"expr":"private-checker-source"}}'
    projected = {**PASS, "_original_reward_json": raw,
                 "reward_sha256": hashlib.sha256(raw.encode()).hexdigest()}
    result = module.reconcile_original(connection, RUN, plan, **options, verifier=lambda: projected)
    assert "private-checker-source" not in json.dumps(result)
    record = read_json(options["store"] / plan / "product-verification.json")
    assert record["payload"]["original_reward_json"] == raw


def test_post_verifier_scope_drift_never_publishes_bound_receipt(completed):
    connection, plan, options, _ = completed

    def verifier():
        connection.db["sale.order"][1]["commitment_date"] = "2026-02-01 00:00:00"
        return PASS

    with pytest.raises(Exception):
        module.reconcile_original(connection, RUN, plan, **options, verifier=verifier)
    assert not (options["store"] / plan / "product-verification.json").exists()


def test_verifier_unknown_outcome_keeps_pending_and_never_restarts_process(completed):
    connection, plan, options, _ = completed
    writes = connection.writes[:]
    with pytest.raises(module.ReconciliationError, match="product_verifier_outcome_unknown"):
        module.reconcile_original(connection, RUN, plan, **options,
                                  verifier=lambda: {"status": "not_run", "strict_success": None})
    assert not (options["store"] / plan / "product-verification.json").exists()
    assert (options["store"] / plan / "product-verification-pending.json").exists()
    with pytest.raises(module.ReconciliationError, match="product_verifier_outcome_unknown"):
        module.reconcile_original(connection, RUN, plan, **options,
                                  verifier=lambda: pytest.fail("unknown child process must not restart"))
    assert connection.writes == writes


def test_proved_not_started_verifier_recovers_after_host_repair_and_preserves_attempt(completed):
    connection, plan, options, _ = completed
    directory = options["store"] / plan
    failed = {"status": "not_run", "strict_success": None, "evaluation_valid": None,
              "error": "verifier_output_not_isolated"}
    with pytest.raises(module.ReconciliationError, match="product_verifier_outcome_unknown"):
        module.reconcile_original(connection, RUN, plan, **options, verifier=lambda: failed)
    before = {path.name: path.read_bytes() for path in directory.glob("product-verification-*.json")}
    db, writes = copy.deepcopy(connection.db), connection.writes[:]
    journal = (directory / "journal.jsonl").read_bytes()
    calls = []

    def verifier():
        calls.append(True)
        with pytest.raises(StorageError, match="another_recovery_operation_running"):
            with scope_lock(options["store"]):
                pass
        return PASS

    result = module.reconcile_original(connection, RUN, plan, **options, verifier=verifier)
    assert result["verifier"] == PASS and len(calls) == 1
    assert module.reconcile_original(connection, RUN, plan, **options, verifier=verifier) == result
    assert len(calls) == 1
    for name, original in before.items():
        if name != "product-verification-pending.json":
            assert (directory / name).read_bytes() == original
    archive, = directory.glob("product-verification-start-*.json")
    assert archive.read_bytes() == before["product-verification-pending.json"]
    pending = read_json(directory / "product-verification-pending.json")
    assert len(pending["retry_of_preflight_attempt_sha256"]) == 64
    assert connection.db == db and connection.writes == writes
    assert (directory / "journal.jsonl").read_bytes() == journal


@pytest.mark.parametrize("fault", ["unknown", "timeout", "different_binding", "missing_proof", "tampered_proof"])
def test_only_attributable_pre_verifier_failure_can_resume(completed, fault):
    connection, plan, options, _ = completed
    directory = options["store"] / plan
    failed = {"status": "not_run", "strict_success": None, "evaluation_valid": None,
              "error": "verifier_output_not_isolated"}
    if fault == "unknown":
        failed["status"] = "unknown"
    elif fault == "timeout":
        failed["error"] = "verifier_outcome_unknown"
    with pytest.raises(module.ReconciliationError, match="product_verifier_outcome_unknown"):
        module.reconcile_original(connection, RUN, plan, **options, verifier=lambda: failed)
    path, = directory.glob("product-verification-attempt-*.json")
    if fault == "missing_proof":
        path.unlink()
    elif fault in ("different_binding", "tampered_proof"):
        proof = read_json(path)
        proof["bindings"]["run_id"] = "local-" + "2" * 32
        if fault == "different_binding":
            path.unlink()
            atomic_json(directory / ("product-verification-attempt-" + digest(proof) + ".json"), proof)
        else:
            atomic_json(path, proof, replace=True)
    with pytest.raises(module.ReconciliationError, match="product_verifier_outcome_unknown"):
        module.reconcile_original(connection, RUN, plan, **options,
                                  verifier=lambda: pytest.fail("unproved verifier attempt must not restart"))
    assert not (directory / "product-verification.json").exists()


def test_pre_verifier_proof_retains_old_host_source_across_repair(completed):
    connection, plan, options, _ = completed
    directory = options["store"] / plan
    failed = {"status": "not_run", "strict_success": None, "evaluation_valid": None,
              "error": "verifier_output_not_isolated"}
    with pytest.raises(module.ReconciliationError):
        module.reconcile_original(connection, RUN, plan, **options, verifier=lambda: failed)
    pending_path = directory / "product-verification-pending.json"
    pending = read_json(pending_path)
    pending["bindings"]["reconciliation_source_sha256"] = "0" * 64
    atomic_json(pending_path, pending, replace=True)
    path, = directory.glob("product-verification-attempt-*.json")
    proof = read_json(path)
    proof["bindings"] = pending["bindings"]
    path.unlink()
    atomic_json(directory / ("product-verification-attempt-" + digest(proof) + ".json"), proof)
    result = module.reconcile_original(connection, RUN, plan, **options, verifier=lambda: PASS)
    assert result["verifier"] == PASS
    archive, = directory.glob("product-verification-start-*.json")
    assert read_json(archive)["bindings"]["reconciliation_source_sha256"] == "0" * 64


def test_guard_connection_rejects_writes_before_transport():
    class Connection:
        def get_model(self, name):
            return object()
    with pytest.raises(module.ReconciliationError, match="reconciliation_write_forbidden"):
        module.ReadOnlyConnection(Connection()).get_model("purchase.order").write(ids=[1], vals={})

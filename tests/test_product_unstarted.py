"""Explicit no-dispatch cancellation; synthetic ERP only, with real root locks."""
from __future__ import annotations

import copy
import json
import multiprocessing
import os
from datetime import UTC, datetime, timedelta

import pytest
import test_product_cases as api_tests
import test_product_local_versions as local_tests
import test_product_versions as root_tests

from forgeflow.api.case_qa import answer_case
from forgeflow.executor.protocol import ProtocolError
from forgeflow.product import ProductCaseError, ProductCaseService, unstarted
from forgeflow.recovery import client, scope_guard, service
from forgeflow.recovery.journal import ExecutionStopped, Journal
from forgeflow.recovery.product_versions import ProductVersions, VersionError
from forgeflow.recovery.readback import ReadbackAdapter
from forgeflow.recovery.storage import atomic_json, read_json, scope_lock

system = root_tests.system
serve = root_tests.serve
workspace = root_tests.workspace
handlers = root_tests.handlers
wired = local_tests.wired


def authorized(system):
    decision = root_tests.prepare(system)
    options = system[2]
    result = root_tests.authorize(options, decision)
    grant = root_tests.grant_from(result["approval"])
    params = dict(run_id=root_tests.RUN, owner_sha256=root_tests.OWNER,
                  plan_id=decision["selected_plan_id"], approval={
                      k: v for k, v in result["approval"].items() if k != "schema_version"},
                  approvals=options["store"].parent / "approvals", **options)
    return params, grant


def stop_before_dispatch(system, params, grant, monkeypatch):
    append = Journal.append

    def crash(history, event, data):
        if event == "dispatch":
            raise KeyboardInterrupt("controlled crash after consume, before dispatch record")
        return append(history, event, data)

    with monkeypatch.context() as patch:
        patch.setattr(Journal, "append", crash)
        with pytest.raises(KeyboardInterrupt):
            service.apply(system[0], params["plan_id"], grant=grant, **system[2])
    assert system[0].writes == []


@pytest.mark.parametrize("consumed", [False, True])
def test_cancel_prevents_late_apply_and_replays_identical_receipt(system, monkeypatch, consumed):
    params, grant = authorized(system)
    if consumed:
        stop_before_dispatch(system, params, grant, monkeypatch)
    receipt = unstarted.cancel_unstarted(**params)
    assert receipt["outcome"] == "not_started" and receipt["approval_invalidated"] is True
    assert unstarted.cancel_unstarted(**params) == receipt
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(system[0], params["plan_id"], grant=grant, **system[2])
    state = ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)
    assert state["state"] == "retired" and state["selected_plan_id"] is None
    assert "unstarted_cancellation" not in json.dumps(state)
    assert not (params["store"] / "active-plan.json").exists()
    assert system[0].writes == []


@pytest.mark.parametrize("journal", ["missing", "empty"])
def test_spent_token_without_durable_plan_row_is_unknown_not_unstarted(system, monkeypatch, journal):
    params, grant = authorized(system)
    stop_before_dispatch(system, params, grant, monkeypatch)
    path = params["store"] / params["plan_id"] / "journal.jsonl"
    path.unlink() if journal == "missing" else path.write_bytes(b"")
    before = ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)
    with pytest.raises(VersionError, match="product_cancellation_unconfirmed"):
        unstarted.cancel_unstarted(**params)
    assert ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER) == before
    assert system[0].writes == []


@pytest.mark.parametrize("completed", [False, True])
def test_any_dispatch_refuses_cancellation_even_without_success(system, completed):
    params, grant = authorized(system)
    if completed:
        service.apply(system[0], params["plan_id"], grant=grant, **system[2])
    else:
        system[0].fail = "before"
        with pytest.raises(ExecutionStopped):
            service.apply(system[0], params["plan_id"], grant=grant, **system[2])
    before = ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)
    writes = list(system[0].writes)
    with pytest.raises(VersionError, match="product_dispatch_present"):
        unstarted.cancel_unstarted(**params)
    assert ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER) == before
    assert system[0].writes == writes


@pytest.mark.parametrize("corruption", ["journal_link", "journal_fifo", "journal_json", "marker", "success_link",
                                         "token", "owner", "nonce", "source", "policy"])
def test_corruption_or_wrong_binding_never_retires_plan(system, corruption):
    params, _ = authorized(system)
    before = ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)
    directory = params["store"] / params["plan_id"]
    if corruption == "journal_link":
        (directory / "journal.jsonl").symlink_to(directory / "absent")
    elif corruption == "journal_fifo":
        os.mkfifo(directory / "journal.jsonl")
    elif corruption == "journal_json":
        (directory / "journal.jsonl").write_bytes(b"{broken\n")
    elif corruption == "success_link":
        (directory / "success.json").symlink_to(directory / "absent")
    elif corruption == "marker":
        atomic_json(params["store"] / "active-plan.json", {"plan_id": "../invalid"})
    elif corruption == "token":
        (params["approvals"] / f"approval-{params['approval']['nonce']}.json").unlink()
    elif corruption == "owner":
        params["owner_sha256"] = "f" * 64
    elif corruption == "nonce":
        params["approval"]["nonce"] = "../invalid"
    elif corruption == "source":
        path = params["bundle"] / "forgeflow/recovery/odoo.py"
        path.chmod(0o600)
        path.write_bytes(path.read_bytes() + b"\n# source drift\n")
    else:
        atomic_json(directory / "product-policy.json", {"changed": True}, replace=True)
    with pytest.raises(Exception):
        unstarted.cancel_unstarted(**params)
    assert ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER) == before
    assert system[0].writes == []


def test_expired_original_grant_can_still_be_invalidated(system, monkeypatch):
    params, _ = authorized(system)

    class Later(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(UTC) + timedelta(days=1)

    monkeypatch.setattr(unstarted, "datetime", Later)
    assert unstarted.cancel_unstarted(**params)["outcome"] == "not_started"


def test_crash_after_retirement_keeps_late_apply_blocked_then_finishes_cleanup(system, monkeypatch):
    params, grant = authorized(system)
    marker = params["store"] / "active-plan.json"
    atomic_json(marker, {"plan_id": params["plan_id"]})
    clear = unstarted._clear_own_marker

    def crash(*args):
        raise OSError("controlled stop before clearing marker")

    monkeypatch.setattr(unstarted, "_clear_own_marker", crash)
    with pytest.raises(OSError):
        unstarted.cancel_unstarted(**params)
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(system[0], params["plan_id"], grant=grant, **system[2])
    monkeypatch.setattr(unstarted, "_clear_own_marker", clear)
    assert unstarted.cancel_unstarted(**params)["outcome"] == "not_started"
    assert not marker.exists() and system[0].writes == []


def test_foreign_active_marker_is_preserved(system):
    params, _ = authorized(system)
    marker = params["store"] / "active-plan.json"
    atomic_json(marker, {"plan_id": "e" * 64})
    unstarted.cancel_unstarted(**params)
    assert read_json(marker) == {"plan_id": "e" * 64}


def _hold_root_lock(store, ready, release):
    with scope_lock(store):
        ready.set()
        release.wait(8)


def test_other_process_lock_blocks_cancellation_without_changing_authority(system):
    params, _ = authorized(system)
    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    child = context.Process(target=_hold_root_lock, args=(params["store"], ready, release))
    child.start()
    try:
        assert ready.wait(8)
        with pytest.raises(Exception, match="another_recovery_operation_running"):
            unstarted.cancel_unstarted(**params)
    finally:
        release.set()
        child.join(8)
        if child.is_alive():
            child.terminate()
            child.join(3)
    assert child.exitcode == 0
    assert ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)["state"] == "approved"


def _race_apply_and_cancel(action, connection, params, grant, start, queue):
    # Each spawned process independently reads the same protected registry.
    # The ERP adapter is invented in both processes; only apply's worker writes.
    scope_guard.collect_snapshot = lambda client, request: connection.collect(request)
    original_apply = ReadbackAdapter.apply

    def apply(adapter, step):
        if step.kind == "bind_workorder":
            return original_apply(adapter, step)
        connection.writes.append(step.key)
        connection.apply(adapter, step)

    ReadbackAdapter.apply = apply
    start.wait(8)
    try:
        if action == "cancel":
            unstarted.cancel_unstarted(**params)
        else:
            service.apply(connection, params["plan_id"], grant=grant,
                          store=params["store"], bundle=params["bundle"])
        queue.put((action, "passed", connection.writes))
    except Exception as error:
        queue.put((action, str(error), connection.writes))


def test_real_process_apply_and_cancellation_have_only_one_winner(system):
    params, grant = authorized(system)
    context = multiprocessing.get_context("spawn")
    start, queue = context.Event(), context.Queue()
    children = [context.Process(target=_race_apply_and_cancel,
                                args=(action, system[0], params, grant, start, queue))
                for action in ("apply", "cancel")]
    for child in children:
        child.start()
    start.set()
    try:
        results = [queue.get(timeout=10) for _ in children]
    finally:
        for child in children:
            child.join(8)
            if child.is_alive():
                child.terminate()
                child.join(3)
        queue.close()
        queue.join_thread()
    assert all(child.exitcode == 0 for child in children)
    winners = [row[0] for row in results if row[1] == "passed"]
    assert len(winners) == 1, results
    assert all(row[1] in {"passed", "another_recovery_operation_running", "product_plan_retired",
                          "product_dispatch_present"} for row in results), results
    state = ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)
    if winners[0] == "cancel":
        assert state["state"] == "retired" and all(not row[2] for row in results)
    else:
        assert state["state"] == "executed" and any(row[2] for row in results)


def interrupted_case(wired, monkeypatch):
    cases, case_id, executor, connection, _, _, _ = wired
    prepared = cases.prepare(case_id, local_tests.ACTOR)
    write = cases._write

    def crash(case):
        write(case)
        if (case.approval or {}).get("phase") == "dispatching":
            raise KeyboardInterrupt("controlled pre-transport crash")

    with monkeypatch.context() as patch:
        patch.setattr(cases, "_write", crash)
        with pytest.raises(KeyboardInterrupt):
            cases.approve(case_id, local_tests.ACTOR, prepared["plan"]["plan_id"])
    assert connection.writes == []
    return ProductCaseService(cases.root.parent, executor), prepared


def test_real_socket_cancellation_after_api_restart_never_opens_erp(wired, monkeypatch):
    cases, case = interrupted_case(wired, monkeypatch)
    root = wired[-1]
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("cancellation cannot open ERP"))
    monkeypatch.setattr(root, "product_verifier", lambda: pytest.fail("cancellation cannot verify"))
    plan = case["plan"]["plan_id"]
    assert cases.get(case["case_id"], local_tests.ACTOR)["decision"]["can_cancel_unstarted"] is True
    result = cases.cancel_unstarted(case["case_id"], local_tests.ACTOR, plan)
    assert result["result"]["outcome"] == "not_started"
    assert result["result"]["verified"] is False
    assert not result["decision"]["can_reconcile"] and not result["decision"]["can_cancel_unstarted"]
    assert cases.cancel_unstarted(case["case_id"], local_tests.ACTOR, plan) == result
    assert cases.reconcile(case["case_id"], local_tests.ACTOR) == result
    assert sum(row["action"] == "unstarted_cancelled" for row in result["audit"]) == 1
    assert wired[3].writes == [] and wired[-2] == []


def test_lost_cancellation_response_replays_same_receipt_after_api_restart(wired, monkeypatch):
    cases, case = interrupted_case(wired, monkeypatch)
    plan = case["plan"]["plan_id"]
    real = client.product
    receipts = []

    def lost(*args, **kwargs):
        response = real(*args, **kwargs)
        receipts.append(copy.deepcopy(response["receipt"]))
        raise client.ExecutorError("unavailable")

    monkeypatch.setattr(client, "product", lost)
    with pytest.raises(ProductCaseError, match="unavailable"):
        cases.cancel_unstarted(case["case_id"], local_tests.ACTOR, plan)
    assert cases.get(case["case_id"], local_tests.ACTOR)["state"] == "executing"
    monkeypatch.setattr(client, "product", real)
    restarted = ProductCaseService(cases.root.parent, wired[2])
    restarted.cancel_unstarted(case["case_id"], local_tests.ACTOR, plan)
    assert restarted._read(case["case_id"]).approval["unstarted_cancellation"] == receipts[0]
    assert wired[3].writes == []


def test_autonomous_root_rejects_cancellation_before_connection(wired, monkeypatch):
    root = wired[-1]
    root.require_grant = False
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("must not connect"))
    with pytest.raises(ProtocolError) as caught:
        root.product("test", "cancel_unstarted", {})
    assert caught.value.code == "product_operations_disabled"
    assert not (wired[4]["bundle"] / "forgeflow/product/unstarted.py").exists()


@pytest.mark.parametrize("damage", ["used_fifo", "used_hash", "journal_hash", "journal_plan"])
def test_consumed_but_corrupt_evidence_keeps_authority_locked(system, monkeypatch, damage):
    params, grant = authorized(system)
    stop_before_dispatch(system, params, grant, monkeypatch)
    path = params["approvals"] / f"approval-{grant.approval.nonce}.used"
    if damage == "used_fifo":
        path.unlink()
        os.mkfifo(path)
    elif damage == "used_hash":
        path.write_text("0" * 64)
    else:
        path = params["store"] / params["plan_id"] / "journal.jsonl"
        if damage == "journal_hash":
            path.write_text(path.read_text().replace('"step_count":', '"changed_count":'))
        else:
            path.unlink()
            with Journal(path) as journal:
                journal.append("plan", {"schema_version": 1, "plan_sha256": "f" * 64, "step_count": 1})
    with pytest.raises(Exception):
        unstarted.cancel_unstarted(**params)
    assert ProductVersions(params["store"]).status(root_tests.RUN, root_tests.OWNER)["state"] == "executing"
    assert system[0].writes == []


def test_wrong_cancellation_receipt_keeps_case_unconfirmed(wired, monkeypatch):
    cases, case = interrupted_case(wired, monkeypatch)
    monkeypatch.setattr(wired[2], "cancel_unstarted", lambda *args: {"outcome": "not_started"})
    with pytest.raises(ProductCaseError, match="product_cancellation_unconfirmed"):
        cases.cancel_unstarted(case["case_id"], local_tests.ACTOR, case["plan"]["plan_id"])
    assert cases.get(case["case_id"], local_tests.ACTOR)["state"] == "executing"
    assert wired[3].writes == []


@pytest.mark.parametrize("question,phrase", [("执行结果是什么？", "此案例已关闭，尚未执行"),
                                            ("What happened during execution?", "closed without execution")])
def test_closed_case_answer_does_not_claim_execution_or_verification(wired, monkeypatch, question, phrase):
    cases, case = interrupted_case(wired, monkeypatch)
    closed = cases.cancel_unstarted(case["case_id"], local_tests.ACTOR, case["plan"]["plan_id"])
    answer = answer_case(question, closed)
    assert phrase in answer["answer"]
    assert "执行结束" not in answer["answer"] and "Execution finished" not in answer["answer"]
    assert answer["citations"]


def test_api_cancellation_auth_owner_schema_and_review_gate(wired, monkeypatch, tmp_path):
    # Real HTTP service -> Local -> Unix socket. ERP and verifier remain synthetic.
    root = tmp_path / "api"
    with api_tests.client_for(root, wired[2]) as (http, _):
        assert http.post(f"/api/product/cases/{'a' * 32}/cancel-unstarted",
                         json={"plan_id": "b" * 64}).status_code == 401
        actor = api_tests.sign_in(http)
        cases = http.app.state.product_cases
        created = cases.create(actor, "B")
        saved = cases._read(created["case_id"])
        saved.order["units"] = 10
        cases._write(saved)
        prepared = cases.prepare(created["case_id"], actor)
        assert prepared["decision"]["can_approve"], prepared["plan"]["checks"]
        plan = prepared["plan"]["plan_id"]
        write = cases._write

        def crash(case):
            write(case)
            if (case.approval or {}).get("phase") == "dispatching":
                raise KeyboardInterrupt("controlled HTTP service restart")

        with monkeypatch.context() as patch:
            patch.setattr(cases, "_write", crash)
            with pytest.raises(KeyboardInterrupt):
                cases.approve(created["case_id"], actor, plan)
        url = f"/api/product/cases/{created['case_id']}/cancel-unstarted"
        assert http.post(url, json={"plan_id": plan, "ignored": True}).status_code == 422
        assert http.post(url, json={"plan_id": "wrong"}).status_code == 422
        api_tests.sign_in(http, api_tests.INVITE_B)
        assert http.post(url, json={"plan_id": plan}).status_code == 404
        api_tests.sign_in(http)
        result = http.post(url, json={"plan_id": plan})
        assert result.status_code == 200, result.text
        assert result.json()["result"]["outcome"] == "not_started"
        assert all(key not in result.text for key in ("executor_grant", "owner_sha256", "nonce"))
    with api_tests.client_for(root, wired[2], deployment="review") as (http, _):
        api_tests.sign_in(http)
        denied = http.post(url, json={"plan_id": plan})
        assert denied.status_code == 403 and denied.json()["detail"] == "review_read_only"
        assert not http.get(url.rsplit("/", 1)[0]).json()["decision"]["can_cancel_unstarted"]
    assert wired[3].writes == [] and wired[-2] == []

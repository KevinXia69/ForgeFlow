"""Fresh versions after a reserved revision failure; synthetic ERP only."""
from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor

import pytest
import test_product_cases as api_tests
import test_product_revision_recovery as revisions

from forgeflow.api.case_qa import answer_case
from forgeflow.api.evidence import public_evidence
from forgeflow.planning.tool import SolverFailure
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.recovery import client, service
from forgeflow.recovery.product_versions import ProductVersions, VersionError, owner_scope
from forgeflow.recovery.storage import read_json

system = revisions.system
serve = revisions.serve
workspace = revisions.workspace
handlers = revisions.handlers
wired = revisions.wired
revision_system = revisions.revision_system
ACTOR = revisions.ACTOR
TRACE = {"solve_calls": [{"status": "INFEASIBLE", "phase": "primary"}], "private-canary": True}


@pytest.fixture
def failed_revision(revision_system, monkeypatch):
    wired, first = revision_system

    def fail(*args):
        raise SolverFailure("planning_model_infeasible", TRACE)

    with monkeypatch.context() as patch:
        patch.setattr(service, "solve_blueprint", fail)
        failed = revisions.revise(wired[0], first)
    assert failed["reason"] == "planning_model_infeasible" and failed["decision"]["can_reprepare"]
    return wired, first, failed


def retry(cases, case_id, version=2):
    return cases.reprepare(case_id, ACTOR, version, reason="Read current facts again")


def test_new_version_keeps_failed_evidence_constraints_and_old_retirement(failed_revision, monkeypatch):
    (cases, case_id, executor, connection, options, verifications, _), first, failed = failed_revision
    before = copy.deepcopy(cases._read(case_id).preparation)
    # A changed source fact must be collected again, not copied from v1.
    connection.db["product.product"][1]["name"] = "Current FIN description"
    source_scope = read_json(options["store"] / first["plan"]["plan_id"] / "snapshot.json")["scope"]
    monkeypatch.setattr(service, "collect_snapshot", lambda _client, request: {
        **connection.collect(request), "scope": copy.deepcopy(source_scope)})
    counts = revisions.record_preparations(monkeypatch)
    result = retry(cases, case_id)
    assert result["state"] == "awaiting_approval" and result["revision"]["version"] == 3, (result["reason"], result["revision"])
    assert result["revision"]["changes"] == revisions.CHANGES
    snapshot = read_json(options["store"] / result["plan"]["plan_id"] / "snapshot.json")
    assert snapshot["models"]["product.product"][0]["name"] == "Current FIN description"
    assert result["plan"]["snapshot_sha256"] != first["plan"]["snapshot_sha256"]
    assert result["history"] == failed["history"]  # no second retirement or invented v2 plan
    assert counts == {"solve": 1, "snapshot": 1}
    assert retry(cases, case_id) == result and counts["solve"] == 1
    assert cases._read(case_id).preparation_history == [before]
    archive = options["store"] / "product-decisions" / f"local-{case_id}-preparations" / "v2.json"
    prior = read_json(archive)
    assert prior["last_operation"]["solver_trace"] == TRACE
    assert prior["state"] == "prepare_failed" and prior["constraints"] == revisions.CHANGES
    public = json.dumps(public_evidence(cases._read(case_id).to_record()))
    assert "private-canary" not in public and "preparation_history" not in public
    assert connection.writes == [] and not list(executor.approvals.glob("approval-*.json"))
    with pytest.raises(VersionError, match="product_plan_retired"):
        service.apply(connection, first["plan"]["plan_id"], **options)
    plan_id = result["plan"]["plan_id"]
    approved = cases.approve(case_id, ACTOR, plan_id)
    assert approved["state"] == "succeeded", approved
    writes = connection.writes[:]
    assert cases.approve(case_id, ACTOR, plan_id) == approved
    assert connection.writes == writes and verifications == [True]


@pytest.mark.parametrize("lost_at", ["before_transport", "after_root_reply", "bad_response"])
def test_pending_reprepare_recovers_same_operation_after_restart(failed_revision, monkeypatch, lost_at):
    (cases, case_id, executor, connection, _, _, root), _, failed = failed_revision
    counts = revisions.record_preparations(monkeypatch)
    product = client.product
    reserving = False

    def lost(*args, **kwargs):
        nonlocal reserving
        if args[2] == "status" and not reserving:  # factory's read-only proof
            return product(*args, **kwargs)
        reserving = True
        if args[2] == "prepare" and lost_at != "before_transport":
            product(*args, **kwargs)
        raise client.ExecutorError("bad_response" if lost_at == "bad_response" else "unavailable")

    monkeypatch.setattr(client, "product", lost)
    pending = retry(cases, case_id)
    assert pending["decision"]["repreparation_pending"] and pending["decision"]["can_resume_repreparation"]
    assert not pending["decision"]["can_reprepare"] and not pending["decision"]["can_resume_preparation"]
    assert pending["plan"] is None and pending["history"] == failed["history"]
    assert pending["revision"]["version"] == 2 and pending["repreparation_request"]["version"] == 3
    assert cases.get(case_id, ACTOR) == pending and cases.list_for(ACTOR) == [pending]
    assert counts["solve"] == (0 if lost_at == "before_transport" else 1)
    with pytest.raises(ProductCaseError):
        cases.approve(case_id, ACTOR, "a" * 64)
    with pytest.raises(ProductCaseError):
        cases.reprepare(case_id, ACTOR, 2, reason="A different request")
    monkeypatch.setattr(client, "product", product)
    if lost_at != "before_transport":
        monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("completed replay cannot open ERP"))
    restarted = ProductCaseService(cases.root.parent, executor)
    result = restarted.prepare(case_id, ACTOR)
    assert result["state"] == "awaiting_approval" and result["revision"]["version"] == 3
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []
    assert sum(row["action"] == "repreparation_requested" for row in result["audit"]) == 1


@pytest.mark.parametrize("crash_at", ["intent_saved", "before_commit", "after_commit"])
def test_reprepare_api_crashes_do_not_duplicate_versions(failed_revision, monkeypatch, crash_at):
    (cases, case_id, executor, connection, _, _, root), _, _ = failed_revision
    counts = revisions.record_preparations(monkeypatch)
    write = cases._write

    def crash(case):
        matches = ((crash_at == "intent_saved" and case.preparation["phase"] == "pending")
                   or (crash_at != "intent_saved" and case.revision["version"] == 3))
        if not matches:
            return write(case)
        if crash_at != "before_commit":
            write(case)
        raise KeyboardInterrupt("controlled restart")

    monkeypatch.setattr(cases, "_write", crash)
    with pytest.raises(KeyboardInterrupt):
        retry(cases, case_id)
    if crash_at != "intent_saved":
        monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("no repeat solve"))
    result = ProductCaseService(cases.root.parent, executor).prepare(case_id, ACTOR)
    assert result["state"] == "awaiting_approval" and result["revision"]["version"] == 3
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []


def test_repeated_failed_floors_never_relax_or_reactivate_old_plan(revision_system, monkeypatch):
    (cases, case_id, _, connection, options, _, _), first = revision_system
    failed = revisions.revise(cases, first, {"minimum_margin_pct": 95})
    assert failed["state"] == "failed"
    counts = revisions.record_preparations(monkeypatch)
    for previous in (2, 3):
        result = retry(cases, case_id, previous)
        assert result["state"] == "failed" and result["revision"]["version"] == previous + 1
        assert result["revision"]["changes"] == {"minimum_margin_pct": 95.0}
        assert result["decision"]["can_reprepare"] and result["plan"] is None
        assert retry(cases, case_id, previous) == result
    assert len(cases._read(case_id).preparation_history) == 2
    assert result["history"] == failed["history"] and counts == {"solve": 2, "snapshot": 2}
    root = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert root["selected_plan_id"] is None and root["plans"][first["plan"]["plan_id"]]["state"] == "retired"
    assert connection.writes == []


@pytest.mark.parametrize("tamper", ["request", "constraints", "origin"])
def test_pending_reprepare_binding_changes_refused_before_root(failed_revision, monkeypatch, tamper):
    (cases, case_id, executor, _, _, _, _), _, _ = failed_revision
    product = client.product

    def before_transport(*args, **kwargs):
        if args[2] == "prepare":
            raise client.ExecutorError("unavailable")
        return product(*args, **kwargs)

    monkeypatch.setattr(client, "product", before_transport)
    pending = retry(cases, case_id)
    assert pending["decision"]["repreparation_pending"]
    if tamper == "request":
        executor.request["margin"] = 0.25
    else:
        case = cases._read(case_id)
        if tamper == "constraints":
            case.preparation["intent"]["constraints"] = {}
        else:
            case.preparation["origin"]["inputs_sha256"] = "a" * 64
        cases._write(case)
    monkeypatch.setattr(client, "product", lambda *args, **kwargs: pytest.fail("must refuse before RPC"))
    result = cases.prepare(case_id, ACTOR)
    assert result["repreparation_request"]["error"] == "product_binding_changed"


def test_two_api_instances_create_only_one_fresh_version(failed_revision, monkeypatch):
    (cases, case_id, executor, connection, _, _, _), _, _ = failed_revision
    counts = revisions.record_preparations(monkeypatch)
    instances = [ProductCaseService(cases.root.parent, executor) for _ in range(2)]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda instance: retry(instance, case_id), instances))
    assert results[0] == results[1] and results[0]["revision"]["version"] == 3
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []


@pytest.mark.parametrize("damage", ["file", "symlink", "fifo"])
def test_failed_archive_is_never_overwritten_or_followed(failed_revision, monkeypatch, damage):
    (cases, case_id, _, connection, options, _, _), _, _ = failed_revision
    directory = options["store"] / "product-decisions" / f"local-{case_id}-preparations"
    directory.mkdir()
    archive = directory / "v2.json"
    if damage == "file":
        archive.write_text('{"corrupt":true}')
    elif damage == "symlink":
        archive.symlink_to(directory / "absent.json")
    else:
        os.mkfifo(archive)
    monkeypatch.setattr(service, "solve_blueprint", lambda *args: pytest.fail("archive failure cannot solve"))
    result = retry(cases, case_id)
    assert result["decision"]["repreparation_pending"]
    assert result["repreparation_request"]["error"] == "product_decision_corrupt", result
    root = ProductVersions(options["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert root["version"] == 2 and root["state"] == "prepare_failed" and connection.writes == []


def test_reprepare_request_schema_and_disabled_capability(failed_revision, tmp_path):
    (cases, case_id, executor, _, _, _, _), _, failed = failed_revision
    with api_tests.client_for(tmp_path / "http", executor) as (http, _):
        api_tests.sign_in(http)
        url = f"/api/product/cases/{case_id}/reprepare"
        body = {"failed_version": 2, "reason": "Read current facts again"}
        assert http.post(url, json=body).status_code == 404
        for invalid in (True, "2", 0):
            assert http.post(url, json={**body, "failed_version": invalid}).status_code == 422
        assert http.post(url, json={**body, "changes": {}}).status_code == 422
    executor.supports_revisions = False
    assert not cases.get(case_id, ACTOR)["decision"]["can_reprepare"]
    with pytest.raises(ProductCaseError, match="revision_unsupported"):
        retry(cases, case_id)
    assert cases._read(case_id).revision == failed["revision"]


def test_http_reprepare_owner_review_resume_and_private_history(revision_system, monkeypatch, tmp_path):
    (__, _, executor, connection, _, _, _), _ = revision_system
    product = client.product
    with api_tests.client_for(tmp_path / "http", executor) as (http, _):
        actor = api_tests.sign_in(http)
        cases = http.app.state.product_cases
        created = cases.create(actor, "B")
        case_id = created["case_id"]
        record = cases._read(case_id)
        record.order["units"] = 10
        cases._write(record)
        first = cases.prepare(case_id, actor)
        url = f"/api/product/cases/{case_id}"

        def fail(*args):
            raise SolverFailure("planning_model_infeasible", TRACE)

        with monkeypatch.context() as patch:
            patch.setattr(service, "solve_blueprint", fail)
            failed = http.post(url + "/revision", json={"plan_id": first["plan"]["plan_id"],
                "reason": "Tighter margin", "changes": revisions.CHANGES}).json()
        assert failed["state"] == "failed" and failed["decision"]["can_reprepare"]
        body = {"failed_version": 2, "reason": "Read current facts again"}
        api_tests.sign_in(http, api_tests.INVITE_B)
        assert http.post(url + "/reprepare", json=body).status_code == 404
        api_tests.sign_in(http)
        started = False

        def lose_reply(*args, **kwargs):
            nonlocal started
            if args[2] == "status" and not started:
                return product(*args, **kwargs)
            if args[2] == "prepare":
                started = True
                product(*args, **kwargs)
            raise client.ExecutorError("unavailable")

        monkeypatch.setattr(client, "product", lose_reply)
        response = http.post(url + "/reprepare", json=body)
        assert response.status_code == 200 and response.json()["decision"]["can_resume_repreparation"]
        for private in ("preparation_history", "inputs_sha256", "private-canary", "executor_binding"):
            assert private not in response.text
    with api_tests.client_for(tmp_path / "http", executor, deployment="review") as (http, _):
        api_tests.sign_in(http)
        assert http.post(url + "/reprepare", json=body).status_code == 403
        assert http.post(url + "/prepare").status_code == 503
        assert http.get(url).json()["decision"]["can_resume_repreparation"] is False
    monkeypatch.setattr(client, "product", product)
    with api_tests.client_for(tmp_path / "http", executor) as (http, _):
        api_tests.sign_in(http)
        result = http.post(url + "/prepare").json()
        assert result["state"] == "awaiting_approval" and result["revision"]["version"] == 3
        assert http.post(url + "/reprepare", json=body).json() == result
    assert connection.writes == []


@pytest.mark.parametrize("failure", ["solver", "reservation_interrupted"])
def test_initial_reserved_failure_can_start_fresh_version(wired, monkeypatch, failure):
    cases, case_id, executor, connection, options, _, _ = wired
    executor.supports_revisions = True
    revisions.bind_synthetic_solver(monkeypatch)
    save = ProductVersions._save

    def fail(*args):
        raise SolverFailure("planning_model_infeasible", TRACE)

    def interrupt(registry, value):
        save(registry, value)
        if value["state"] == "preparing":
            raise RuntimeError("controlled root reservation interruption")

    with monkeypatch.context() as patch:
        if failure == "solver":
            patch.setattr(service, "solve_blueprint", fail)
        else:
            patch.setattr(ProductVersions, "_save", interrupt)
        first = cases.prepare(case_id, ACTOR)
    if first["state"] == "preparing":
        first = cases.prepare(case_id, ACTOR)  # observes interrupted v1; never re-solves it
    assert first["state"] == "failed" and first["decision"]["can_reprepare"], first
    counts = revisions.record_preparations(monkeypatch)
    result = retry(cases, case_id, 1)
    assert result["state"] == "awaiting_approval" and result["revision"]["version"] == 2, result
    assert result["history"] == [] and result["revision"]["changes"] == {}
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []
    archive = options["store"] / "product-decisions" / f"local-{case_id}-preparations" / "v1.json"
    assert read_json(archive)["last_operation"]["stage"] == ("failed" if failure == "solver" else "pending")


def test_archive_saved_before_reservation_can_be_reused_after_crash(failed_revision, monkeypatch):
    (cases, case_id, _, connection, options, _, _), _, _ = failed_revision
    archive = ProductVersions._archive_failed_preparation
    counts = revisions.record_preparations(monkeypatch)

    def interrupt(registry, value):
        archive(registry, value)
        raise RuntimeError("controlled interruption after archival")

    with monkeypatch.context() as patch:
        patch.setattr(ProductVersions, "_archive_failed_preparation", interrupt)
        pending = retry(cases, case_id)
    assert pending["decision"]["repreparation_pending"]
    assert counts == {"solve": 0, "snapshot": 0}
    result = cases.prepare(case_id, ACTOR)
    assert result["revision"]["version"] == 3 and result["state"] == "awaiting_approval"
    assert counts == {"solve": 1, "snapshot": 1} and connection.writes == []
    assert len(list((options["store"] / "product-decisions" / f"local-{case_id}-preparations").glob("*.json"))) == 1


@pytest.mark.parametrize("question", ["现在怎么样？", "What happened?"])
def test_reprepare_qa_keeps_failed_and_retired_versions_distinct(question):
    case = {"state": "preparing", "reason": "preparation_confirmation_pending",
            "decision": {"repreparation_pending": True}, "revision": {"version": 2},
            "repreparation_request": {"version": 3}, "plan": None}
    response = answer_case(question, case)
    assert "旧方案保持退役" in response["answer"] or "earlier plans remain retired" in response["answer"]
    assert {row["field"] for row in response["citations"]} >= {"revision.version", "repreparation_request.version"}

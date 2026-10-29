"""Durable candidate choice over CaseService/HTTP/Local/root; invented ERP only."""
from __future__ import annotations

import copy
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
import test_product_candidate_local as local_tests
import test_product_cases as api_tests

from forgeflow.api.case_qa import answer_case
from forgeflow.api.evidence import public_evidence
from forgeflow.planning.tool import SolverFailure
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.product.candidate_store import CandidateStore
from forgeflow.recovery import client, service
from forgeflow.recovery.product_versions import ProductVersions, owner_scope
from forgeflow.recovery.storage import atomic_json, read_json

system = local_tests.system
candidates = local_tests.candidates
serve = local_tests.serve
workspace = local_tests.workspace
handlers = local_tests.handlers
wired_candidates = local_tests.wired_candidates
ACTOR = local_tests.ACTOR


@pytest.fixture
def case_service(wired_candidates, tmp_path):
    executor, case, system, calls, plans, root = wired_candidates
    cases = ProductCaseService(tmp_path / "case-store", executor)
    case.preparation = None
    cases._write(case)
    return cases, case.case_id, executor, system, calls, plans, root


def choose(cases, case_id, view, index=1, *, uid=ACTOR):
    comparison = view["comparison"]
    return cases.select_candidate(case_id, uid, comparison["plans"][index]["plan_id"],
        comparison_id=comparison["comparison_id"], selection_revision=comparison["selection_revision"])


def test_case_waits_for_choice_persists_separate_immutable_artifact_then_approves_same_plan(case_service):
    cases, case_id, executor, system, calls, _, root = case_service
    before = copy.deepcopy(system[0].db)
    prepared = cases.prepare(case_id, ACTOR)
    assert prepared["state"] == "awaiting_selection" and prepared["plan"] is None
    assert prepared["decision"]["can_select_candidate"] and not prepared["decision"]["can_approve"]
    stored = cases._read(case_id)
    directory = cases.root / "candidate-sets" / case_id
    paths = list(directory.glob("*.json"))
    assert len(paths) == 1
    original_bytes = paths[0].read_bytes()
    assert len(json.dumps(stored.comparison)) < 400 and "plans" not in stored.comparison
    with pytest.raises(ProductCaseError, match="candidate_selection_required"):
        cases.approve(case_id, ACTOR, prepared["comparison"]["plans"][0]["plan_id"])
    selected = choose(cases, case_id, prepared)
    plan_id = selected["plan"]["plan_id"]
    assert selected["state"] == "awaiting_approval" and selected["decision"]["can_approve"]
    assert selected["comparison"]["selected_plan_id"] == plan_id
    assert paths[0].read_bytes() == original_bytes and len(list(directory.glob("*.json"))) == 1
    assert system[0].db == before and not system[0].writes and len(calls) == 2
    assert not list(executor.approvals.glob("approval-*.json"))
    assert ProductCaseService(cases.root, executor).get(case_id, ACTOR) == selected
    assert choose(cases, case_id, prepared) == selected
    root.product_verifier = lambda: {"status": "passed", "source": "synthetic_executor",
        "strict_success": True, "evaluation_valid": True, "evaluation_scope": "product_original_verifier"}
    complete = cases.approve(case_id, ACTOR, plan_id)
    assert complete["state"] == "succeeded" and complete["result"]["simulation_passed"]
    writes = list(system[0].writes)
    assert writes and cases.approve(case_id, ACTOR, plan_id) == complete and system[0].writes == writes
    assert len(calls) == 2 and paths[0].read_bytes() == original_bytes
    public = json.dumps(public_evidence(complete))
    for forbidden in (ACTOR, owner_scope(ACTOR), "artifact_sha256", "executor_binding", "comparison_sha256", "root_revision"):
        assert forbidden not in public


@pytest.mark.parametrize("stage", ["intent_saved", "reply_lost", "before_case_commit", "after_case_commit"])
def test_selection_restart_recovers_exact_intent_without_read_side_effects(case_service, monkeypatch, stage):
    cases, case_id, executor, system, calls, _, root = case_service
    prepared = cases.prepare(case_id, ACTOR)
    product = client.product
    write = cases._write

    def crash(record):
        if stage == "before_case_commit" and (record.selection or {}).get("phase") == "completed":
            raise KeyboardInterrupt("controlled pre-commit stop")
        write(record)
        if stage == "intent_saved" or stage == "after_case_commit" and (record.selection or {}).get("phase") == "completed":
            raise KeyboardInterrupt("controlled post-save stop")

    with monkeypatch.context() as patch:
        if stage == "reply_lost":
            def lose(*args, **kwargs):
                product(*args, **kwargs)
                raise client.ExecutorError("unavailable")
            patch.setattr(client, "product", lose)
            pending = choose(cases, case_id, prepared)
            assert pending["decision"]["selection_pending"]
        else:
            patch.setattr(cases, "_write", crash)
            with pytest.raises(KeyboardInterrupt):
                choose(cases, case_id, prepared)
    saved = copy.deepcopy(cases._read(case_id).selection)
    with monkeypatch.context() as patch:
        patch.setattr(client, "product", lambda *a, **k: pytest.fail("GET retried root operation"))
        current = cases.get(case_id, ACTOR)
        assert cases.list_for(ACTOR) == [current]
        if stage != "after_case_commit":
            assert current["decision"]["can_resume_selection"] and not current["decision"]["can_approve"]
            with pytest.raises(ProductCaseError, match="selection_confirmation_pending"):
                choose(cases, case_id, prepared, index=0)
            with pytest.raises(ProductCaseError, match="selection_confirmation_pending"):
                cases.reject(case_id, ACTOR)
            with pytest.raises(ProductCaseError, match="selection_confirmation_pending"):
                cases.approve(case_id, ACTOR, prepared["comparison"]["plans"][0]["plan_id"])
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("selection opened ERP"))
    restarted = ProductCaseService(cases.root, executor)
    selected = choose(restarted, case_id, prepared)
    assert selected["state"] == "awaiting_approval" and not selected["decision"]["selection_pending"]
    assert restarted._read(case_id).selection["intent"] == saved["intent"]
    assert sum(row["action"] == "selection_requested" for row in selected["audit"]) == 1
    assert sum(row["action"] == "candidate_selected" for row in selected["audit"]) == 1
    assert len(calls) == 2 and not system[0].writes


def test_two_service_instances_race_one_choice_and_stale_choice_cannot_replace_it(case_service):
    cases, case_id, executor, system, calls, _, _ = case_service
    prepared = cases.prepare(case_id, ACTOR)
    other = ProductCaseService(cases.root, executor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda instance: choose(instance, case_id, prepared), (cases, other)))
    assert results[0] == results[1]
    with pytest.raises(ProductCaseError, match="product_revision_conflict"):
        choose(cases, case_id, prepared, index=0)
    replacement = choose(cases, case_id, results[0], index=0)
    assert replacement["plan"]["plan_id"] != results[0]["plan"]["plan_id"]
    assert len(calls) == 2 and not system[0].writes
    assert len(list((cases.root / "candidate-sets" / case_id).glob("*.json"))) == 1


@pytest.mark.parametrize("fault", ["missing", "changed", "symlink", "fifo"])
def test_missing_or_corrupt_candidate_artifact_is_readable_as_unknown_but_never_approvable(
        case_service, fault):
    import os

    cases, case_id, _, system, _, _, _ = case_service
    prepared = cases.prepare(case_id, ACTOR)
    selected = choose(cases, case_id, prepared)
    ref = cases._read(case_id).comparison
    path = cases.root / "candidate-sets" / case_id / (ref["artifact_sha256"] + ".json")
    if fault == "changed":
        value = read_json(path)
        value["plans"][0]["margin"] = 100
        atomic_json(path, value, replace=True)
    elif fault == "symlink":
        target = path.with_name("original.json")
        path.rename(target)
        path.symlink_to(target)
    else:
        path.unlink()
        if fault == "fifo":
            os.mkfifo(path)
    current = cases.get(case_id, ACTOR)
    assert current["comparison"]["status"] == "unavailable"
    assert not current["decision"]["can_approve"] and not current["decision"]["can_select_candidate"]
    with pytest.raises(ProductCaseError, match="candidate_evidence_unavailable"):
        cases.approve(case_id, ACTOR, selected["plan"]["plan_id"])
    assert not system[0].writes


def test_unselected_group_can_be_rejected_without_selecting_or_issuing_approval(case_service):
    cases, case_id, executor, system, calls, _, _ = case_service
    prepared = cases.prepare(case_id, ACTOR)
    rejected = cases.reject(case_id, ACTOR, "none fit", plan_id=prepared["comparison"]["plans"][0]["plan_id"])
    assert rejected["state"] == "rejected" and rejected["plan"] is None
    assert cases.reject(case_id, ACTOR, "retry") == rejected
    decision = ProductVersions(system[2]["store"]).status(f"local-{case_id}", owner_scope(ACTOR))
    assert decision["state"] == "retired" and all(row["state"] == "retired" for row in decision["plans"].values())
    assert not list(executor.approvals.glob("approval-*.json")) and not system[0].writes and len(calls) == 2


def test_revision_uses_separate_artifacts_and_preserves_private_history_references(case_service, monkeypatch, tmp_path):
    cases, case_id, executor, system, calls, _, _ = case_service
    prepared = cases.prepare(case_id, ACTOR)
    selected = choose(cases, case_id, prepared)
    original_ref = copy.deepcopy(cases._read(case_id).comparison)
    outputs = {}

    def solve(blueprint, upstream, *, product_objective):
        calls.append(product_objective)
        if not outputs:
            outputs.update(local_tests.candidate_tests.pinned_plans(system[2]["bundle"], blueprint, tmp_path))
        return copy.deepcopy(outputs[product_objective])

    monkeypatch.setattr(service, "solve_blueprint", solve)
    revised = cases.request_revision(case_id, ACTOR, selected["plan"]["plan_id"],
                                    reason="tighten", changes={"minimum_margin_pct": 90})
    assert revised["state"] == "awaiting_selection" and revised["plan"] is None
    assert revised["revision"]["version"] == 2 and len(revised["comparison"]["plans"]) == 2
    old = revised["history"][0]
    assert old["comparison"]["selected_plan_id"] == selected["plan"]["plan_id"]
    assert old["plan"] == selected["plan"]
    raw = cases._read(case_id)
    assert raw.history[0]["comparison"] == original_ref and "plan" not in raw.history[0]
    assert len(list((cases.root / "candidate-sets" / case_id).glob("*.json"))) == 2
    assert json.dumps(public_evidence(revised)).find("artifact_sha256") == -1
    assert ProductCaseService(cases.root, executor).get(case_id, ACTOR) == revised
    assert len(calls) == 4 and not system[0].writes


def test_http_selection_requires_owner_current_version_and_review_permission(wired_candidates, tmp_path):
    executor, _, system, calls, _, root = wired_candidates
    with api_tests.client_for(tmp_path / "http", executor) as (http, settings):
        owner = api_tests.sign_in(http)
        prepared = api_tests.create_case(http, "B")
        assert prepared["state"] == "awaiting_selection"
        case_id = prepared["case_id"]
        comparison = prepared["comparison"]
        body = {"plan_id": comparison["plans"][1]["plan_id"], "comparison_id": comparison["comparison_id"],
                "selection_revision": comparison["selection_revision"]}
        url = f"/api/product/cases/{case_id}/select"
        api_tests.sign_in(http, api_tests.INVITE_B)
        assert http.post(url, json=body).status_code == 404
        assert http.get(f"/api/product/cases/{case_id}").status_code == 404
        api_tests.sign_in(http)
        for payload in ({**body, "selection_revision": True}, {**body, "owner": owner},
                        {**body, "comparison_id": "../private"}):
            assert http.post(url, json=payload).status_code == 422
        selected = http.post(url, json=body)
        assert selected.status_code == 200, selected.text
        assert selected.json()["state"] == "awaiting_approval"
        assert http.post(url, json=body).json() == selected.json()
        assert http.get(f"/api/product/cases/{case_id}").json() == selected.json()
        serialized = selected.text
        for forbidden in (owner, "artifact_sha256", "executor_binding", "comparison_sha256", "root_revision"):
            assert forbidden not in serialized
    with api_tests.client_for(tmp_path / "review", executor, deployment="review") as (http, _):
        api_tests.sign_in(http)
        assert http.post(url, json=body).status_code == 403
    assert len(calls) == 2 and not system[0].writes


@pytest.mark.parametrize("stage", ["artifact_saved", "before_case_commit", "after_case_commit"])
def test_candidate_preparation_commit_recovery_reuses_same_artifact_without_solving(case_service, monkeypatch, stage):
    cases, case_id, executor, system, calls, _, root = case_service
    original_save, original_write = CandidateStore.save, cases._write

    def save(store, *args):
        original_save(store, *args)
        raise OSError("controlled artifact response loss")

    def write(record):
        if stage == "before_case_commit" and record.comparison is not None:
            raise KeyboardInterrupt("controlled case commit failure")
        original_write(record)
        if stage == "after_case_commit" and record.comparison is not None:
            raise KeyboardInterrupt("controlled stop after commit")

    with monkeypatch.context() as patch:
        if stage == "artifact_saved":
            patch.setattr(CandidateStore, "save", save)
            with pytest.raises(ProductCaseError, match="product_store_unavailable"):
                cases.prepare(case_id, ACTOR)
        else:
            patch.setattr(cases, "_write", write)
            with pytest.raises(KeyboardInterrupt):
                cases.prepare(case_id, ACTOR)
    paths = list((cases.root / "candidate-sets" / case_id).glob("*.json"))
    assert len(paths) == 1 and len(calls) == 2
    original_bytes = paths[0].read_bytes()
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("preparation replay opened ERP"))
    recovered = ProductCaseService(cases.root, executor).prepare(case_id, ACTOR)
    assert recovered["state"] == "awaiting_selection"
    assert sum(row["action"] == "prepared" for row in recovered["audit"]) == 1
    assert len(calls) == 2 and not system[0].writes and paths[0].read_bytes() == original_bytes
    assert len(list(paths[0].parent.glob("*.json"))) == 1


def test_failed_candidate_preparation_can_explicitly_create_a_new_version_with_same_objective_set(case_service, monkeypatch):
    cases, case_id, executor, system, calls, _, _ = case_service
    original_solve = service.solve_blueprint

    def no_candidate(*args, **kwargs):
        raise SolverFailure("planning_model_infeasible", {"solve_calls": [{"status": "INFEASIBLE"}]})

    with monkeypatch.context() as patch:
        patch.setattr(service, "solve_blueprint", no_candidate)
        failed = cases.prepare(case_id, ACTOR)
    assert failed["state"] == "failed" and failed["reason"] == "product_candidates_unavailable"
    assert failed["decision"]["can_reprepare"]
    assert service.solve_blueprint is original_solve
    recovered = ProductCaseService(cases.root, executor).reprepare(case_id, ACTOR, 1, reason="read facts again")
    assert recovered["state"] == "awaiting_selection" and recovered["revision"]["version"] == 2
    assert len(recovered["comparison"]["plans"]) == 2
    assert len(cases._read(case_id).preparation_history) == 1
    assert len(calls) == 2 and not system[0].writes


def test_two_real_processes_recover_one_durable_selection_without_second_root_decision(case_service, tmp_path):
    cases, case_id, executor, system, calls, _, _ = case_service
    prepared = cases.prepare(case_id, ACTOR)
    request = tmp_path / "child-request.json"
    atomic_json(request, executor.request)
    comparison = prepared["comparison"]
    config = {"socket_path": executor.socket_path, "store": str(executor.store),
              "approvals": str(executor.approvals), "request": str(request),
              "case_root": str(cases.root), "case_id": case_id, "actor": ACTOR,
              "source_root": str(Path(__file__).resolve().parents[1] / "src"),
              "plan_id": comparison["plans"][1]["plan_id"], "comparison_id": comparison["comparison_id"],
              "selection_revision": comparison["selection_revision"]}
    script = """
import copy,json,sys
from pathlib import Path
cfg=json.loads(sys.argv[1]); sys.path.insert(0,cfg['source_root'])
from forgeflow.product import local,ProductCaseService
request=json.loads(Path(cfg['request']).read_text())
local.SCENARIOS=copy.deepcopy(local.SCENARIOS)
local.SCENARIOS['B'].update(product_code=request['target_product_code'],disruption=request['disruption'])
executor=local.LocalProductExecutor(**{key:cfg[key] for key in ('socket_path','store','approvals','request')},
    container='synthetic-never-docker',scenario='B',enable_candidates=True,enable_revisions=True)
cases=ProductCaseService(cfg['case_root'],executor)
result=cases.select_candidate(cfg['case_id'],cfg['actor'],cfg['plan_id'],
    comparison_id=cfg['comparison_id'],selection_revision=cfg['selection_revision'])
print(json.dumps({'state':result['state'],'plan_id':result['plan']['plan_id']}))
"""
    children = []
    try:
        for _ in range(2):
            children.append(subprocess.Popen([sys.executable, "-I", "-B", "-c", script, json.dumps(config)],
                                              stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        results = []
        for process in children:
            stdout, stderr = process.communicate(timeout=30)
            assert process.returncode == 0, stderr
            results.append(json.loads(stdout))
    finally:
        for process in children:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    assert results == [{"state": "awaiting_approval", "plan_id": config["plan_id"]}] * 2
    current = cases.get(case_id, ACTOR)
    assert sum(row["action"] == "selection_requested" for row in current["audit"]) == 1
    assert sum(row["action"] == "candidate_selected" for row in current["audit"]) == 1
    assert current["comparison"]["selection_revision"] == comparison["selection_revision"] + 1
    assert len(calls) == 2 and not system[0].writes


@pytest.mark.parametrize("question,language", [("Compare the candidates", "en"), ("比较候选方案", "zh")])
def test_candidate_answers_follow_current_selection_and_pending_confirmation(case_service, monkeypatch, question, language):
    cases, case_id, executor, system, calls, _, root = case_service
    prepared = cases.prepare(case_id, ACTOR)
    before = copy.deepcopy(prepared)
    reply = answer_case(question, prepared)
    assert "USD 62.00" in reply["answer"] and "USD 50.00" in reply["answer"]
    assert ("尚未选定方案" if language == "zh" else "No plan has been selected") in reply["answer"]
    assert ("不证明全局最优" if language == "zh" else "not proof of a global optimum") in reply["answer"]
    citations = {row["field"]: row["value"] for row in reply["citations"]}
    assert citations["comparison.plans"] == 2
    assert [citations[f"comparison.plans.{index}.costs.planned.total"] for index in range(2)] == ["62.0", "50.0"]
    assert "comparison.selected_plan_id" not in citations
    for row in reply["citations"]:
        value = prepared
        for key in row["field"].split("."):
            value = value[int(key)] if isinstance(value, list) else value[key]
        assert row["value"] == (len(value) if isinstance(value, list) else value)
    plan_answer = answer_case("方案成本" if language == "zh" else "Plan cost", prepared)
    assert plan_answer == reply
    execution = answer_case("执行结果" if language == "zh" else "Execution result", prepared)
    assert ("等待选择" if language == "zh" else "awaiting a choice") in execution["answer"]
    assert prepared == before
    selected = choose(cases, case_id, prepared)
    assert any(row["field"] == "comparison.selected_plan_id" and row["value"] == selected["plan"]["plan_id"]
               for row in answer_case(question, selected)["citations"])
    selected_answer = answer_case("方案成本" if language == "zh" else "Plan cost", selected)
    selected_citations = {row["field"]: row["value"] for row in selected_answer["citations"]}
    assert selected_citations["plan.new_spend"] == 50
    assert selected_citations["plan.objective"] == selected["plan"]["objective"]
    product = client.product

    def lose(*args, **kwargs):
        product(*args, **kwargs)
        raise client.ExecutorError("unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(client, "product", lose)
        pending = choose(cases, case_id, selected, index=0)
    assert pending["plan"]["plan_id"] == selected["plan"]["plan_id"]
    pending_answer = answer_case(question, pending)
    assert ("上一次确认的选择" if language == "zh" else "last confirmed choice") in pending_answer["answer"]
    assert not any(row["field"].startswith("plan.") for row in pending_answer["citations"])
    assert "USD 50" not in pending_answer["answer"]
    monkeypatch.setattr(root, "connection_factory", lambda: pytest.fail("answer or recovery opened ERP"))
    recovered = choose(ProductCaseService(cases.root, executor), case_id, selected, index=0)
    assert recovered["plan"]["new_spend"] == 62
    assert not system[0].writes and len(calls) == 2


@pytest.mark.parametrize("outcome", ["duplicate", "infeasible"])
def test_one_valid_candidate_is_not_duplicated_and_retains_other_attempt_explanation(case_service, monkeypatch, outcome):
    cases, case_id, _, system, _, plans, _ = case_service
    if outcome == "duplicate":
        plans["min_planned_cost"] = copy.deepcopy(plans["preserve_quantities"])
        plans["min_planned_cost"]["product_objective"] = "min_planned_cost"
        plans["min_planned_cost"]["plan"]["objective_kind"] = "min_new_spend"
    else:
        original = service.solve_blueprint

        def solve(*args, product_objective, **kwargs):
            if product_objective == "min_planned_cost":
                raise SolverFailure("planning_model_infeasible", {"solve_calls": [{"status": "INFEASIBLE"}]})
            return original(*args, product_objective=product_objective, **kwargs)
        monkeypatch.setattr(service, "solve_blueprint", solve)
    prepared = cases.prepare(case_id, ACTOR)
    assert prepared["state"] == "awaiting_approval" and prepared["decision"]["can_approve"]
    assert not prepared["decision"]["can_select_candidate"]
    assert len(prepared["comparison"]["plans"]) == 1
    assert prepared["comparison"]["attempts"][1]["status"] == outcome
    for question in ("Compare candidates", "比较候选"):
        reply = answer_case(question, prepared)
        assert "USD 62.00" in reply["answer"] and "USD 50.00" not in reply["answer"]
        expected = ("未重复展示" if outcome == "duplicate" else "未生成可行候选") if question.startswith("比较") else (
            "no extra candidate" if outcome == "duplicate" else "did not produce a feasible candidate")
        assert expected in reply["answer"]
    assert not system[0].writes


@pytest.mark.parametrize("previous_choice", [None, 0])
def test_root_selection_conflict_preserves_candidates_without_claiming_preparation_failure_or_no_execution(
    case_service, monkeypatch, previous_choice,
):
    cases, case_id, executor, system, calls, _, _ = case_service
    prepared = cases.prepare(case_id, ACTOR)
    if previous_choice is not None:
        prepared = choose(cases, case_id, prepared, index=previous_choice)

    def conflict(*args, **kwargs):
        raise client.ExecutorError("product_revision_conflict")

    monkeypatch.setattr(executor, "continue_selection", conflict)
    failed = choose(cases, case_id, prepared, index=1)
    assert failed["state"] == "failed" and failed["reason"] == "product_selection_conflict"
    assert failed["plan"] == prepared["plan"]
    assert len(failed["comparison"]["plans"]) == 2
    assert not any(failed["decision"][key] for key in (
        "can_approve", "can_select_candidate", "can_resume_selection", "can_request_changes", "can_reprepare"))
    for question in ("执行结果", "比较候选", "Execution result", "Compare candidates"):
        reply = answer_case(question, failed)
        assert ("不证明 ERP" if question in {"执行结果", "比较候选"} else "does not establish") in reply["answer"]
        assert any(row["field"] == "reason" and row["value"] == "product_selection_conflict" for row in reply["citations"])
    assert cases.get(case_id, ACTOR) == failed
    assert len(calls) == 2 and not system[0].writes

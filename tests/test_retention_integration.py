"""Opt-in refinement is persisted and bound by the real recovery service."""
from __future__ import annotations

import copy
import json

import pytest
import test_product_candidate_cases as candidate_case_tests
import test_product_candidate_local as candidate_local_tests
import test_product_local_versions as local_tests
import test_product_versions as version_tests
import test_recovery_service as service_tests
from recovery_synthetic import Fake
from test_planning_retention import two_deadline_facts
from test_product_constraints import bind_synthetic_solver

from forgeflow.planning import retention
from forgeflow.planning.objectives import PRODUCT_OBJECTIVES
from forgeflow.planning.tool import solve_blueprint
from forgeflow.product import ProductCaseError, ProductCaseService
from forgeflow.recovery import service
from forgeflow.recovery.product_constraints import load_constraints
from forgeflow.recovery.product_versions import owner_scope
from forgeflow.recovery.storage import atomic_json, digest, read_json

system = service_tests.system
serve = version_tests.serve
workspace = version_tests.workspace
handlers = version_tests.handlers
wired = local_tests.wired
candidates = candidate_local_tests.candidates
wired_candidates = candidate_local_tests.wired_candidates
case_service = candidate_case_tests.case_service


def artifact(options, prepared, name):
    return read_json(options["store"] / prepared["plan_id"] / f"{name}.json")


def test_default_service_prepare_never_invokes_refinement(system, monkeypatch):
    connection, request, options, _ = system
    monkeypatch.setattr(retention, "refine_retention",
                        lambda *args, **kwargs: pytest.fail("Opt-in was not selected"))
    prepared = service.prepare(connection, request, **options)
    assert "retention_profile" not in artifact(options, prepared, "manifest")
    assert "retention_refinement" not in artifact(options, prepared, "plan")
    assert connection.writes == []


def test_explicit_profile_persists_even_when_original_optimality_is_unproven(system):
    connection, request, options, _ = system
    baseline = service.prepare(connection, request, **options)
    prepared = service.prepare(connection, request, retention_profile=retention.RETENTION_PROFILE,
                               **options)
    manifest = artifact(options, prepared, "manifest")
    plan = artifact(options, prepared, "plan")
    audit = plan["retention_refinement"]
    assert manifest["retention_profile"] == retention.RETENTION_PROFILE
    assert manifest["artifacts"]["plan"] == digest(plan)
    assert prepared["plan_id"] != baseline["plan_id"]
    assert audit["status"] == "skipped" and audit["reason"] == "original_optima_not_proven"
    assert audit["original_plan"] == artifact(options, baseline, "plan")
    assert audit["original_plan_sha256"] == digest(audit["original_plan"])
    assert plan["plan"] == audit["original_plan"]["plan"]
    retention.validate_refinement_audit(plan)
    assert connection.writes == []


def test_real_service_refinement_improves_mapping_and_persists_both_plans(system, monkeypatch):
    connection, _, options, _ = system
    request, snapshot = two_deadline_facts()
    connection.__dict__.update(Fake(snapshot).__dict__)
    monkeypatch.setattr(service, "collect_snapshot", lambda *_: copy.deepcopy(snapshot))
    monkeypatch.setattr(service, "solve_blueprint", solve_blueprint)
    prepared = service.prepare(connection, request, retention_profile=retention.RETENTION_PROFILE,
                               **options)
    manifest = artifact(options, prepared, "manifest")
    plan = artifact(options, prepared, "plan")
    changes = artifact(options, prepared, "change-set")
    audit = plan["retention_refinement"]
    assert audit["status"] == "improved"
    assert audit["retained_before"] == 4 and audit["retained_after"] == 6
    assert changes["projected_state"]["totals"]["retained_manufactured_quantity"] == 6
    assert audit["original_plan"]["plan"]["expected_new_spend"] == plan["plan"]["expected_new_spend"] == 56
    assert manifest["retention_profile"] == audit["profile"] == retention.RETENTION_PROFILE
    assert manifest["artifacts"]["plan"] == digest(plan)
    retention.validate_refinement_audit(plan)
    assert connection.writes == []


@pytest.mark.parametrize("candidates", [False, True])
def test_product_preparation_profile_matches_its_persisted_input_constraints(
    system, monkeypatch, candidates,
):
    connection, request, options, _ = system
    monkeypatch.setattr(service, "solve_blueprint", solve_blueprint)
    decision = service.prepare_product(connection, request, run_id="local-" + "c" * 32,
        owner_sha256=owner_scope("retention-test"), expected_revision=0, operation_id="prepare-1",
        contract=service_tests._contract(), retention_profile=retention.RETENTION_PROFILE,
        product_objectives=list(PRODUCT_OBJECTIVES) if candidates else None, **options)
    for plan_id, row in decision["plans"].items():
        directory = options["store"] / plan_id
        manifest = read_json(directory / "manifest.json")
        assert manifest["retention_profile"] == retention.RETENTION_PROFILE
        assert load_constraints(directory, manifest, request) == {}
        inputs = {"request": request, "policy_sha256": service_tests._contract().digest(),
                  "constraints": {}, "retention_profile": retention.RETENTION_PROFILE}
        if candidates:
            inputs["product_objectives"] = list(PRODUCT_OBJECTIVES)
        assert row["product"]["inputs_sha256"] == digest(inputs)
        retention.validate_refinement_audit(read_json(directory / "plan.json"))
    assert connection.writes == []


@pytest.mark.parametrize("profile", ["unknown", True, {}, [], ""])
def test_unknown_operator_profile_rejected_before_preparation(system, monkeypatch, profile):
    connection, request, options, _ = system
    monkeypatch.setattr(service, "collect_snapshot", lambda *_: pytest.fail("Unexpected ERP read"))
    with pytest.raises(service.RecoveryError, match="invalid_retention_profile"):
        service.prepare(connection, request, retention_profile=profile, **options)
    assert not options["store"].exists()


@pytest.mark.parametrize("product", [False, True])
def test_enabled_profile_requires_refinement_source_in_bundle_manifest(system, monkeypatch, product):
    connection, request, options, _ = system
    provenance = options["bundle"] / "provenance.json"
    sources = read_json(provenance)
    del sources["files_sha256"]["forgeflow/planning/retention.py"]
    atomic_json(provenance, sources, replace=True)
    assert (options["bundle"] / "forgeflow/planning/retention.py").is_file()
    monkeypatch.setattr(service, "collect_snapshot", lambda *_: pytest.fail("Unexpected ERP read"))
    with pytest.raises(service.RecoveryError, match="incomplete_source_manifest"):
        if product:
            service.prepare_product(connection, request, run_id="local-" + "c" * 32,
                owner_sha256=owner_scope("retention-test"), expected_revision=0,
                operation_id="prepare-1", contract=service_tests._contract(),
                retention_profile=retention.RETENTION_PROFILE,
                product_objectives=list(PRODUCT_OBJECTIVES), **options)
        else:
            service.prepare(connection, request, retention_profile=retention.RETENTION_PROFILE,
                            **options)
    assert connection.writes == []


def test_default_profile_accepts_bundle_without_unused_refinement_source(system):
    connection, request, options, _ = system
    provenance = options["bundle"] / "provenance.json"
    sources = read_json(provenance)
    del sources["files_sha256"]["forgeflow/planning/retention.py"]
    atomic_json(provenance, sources, replace=True)
    prepared = service.prepare(connection, request, **options)
    assert "retention_profile" not in artifact(options, prepared, "manifest")
    assert "retention_refinement" not in artifact(options, prepared, "plan")
    assert connection.writes == []


def test_apply_rejects_self_consistent_profile_plan_without_refinement_source(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, retention_profile=retention.RETENTION_PROFILE,
                               **options)
    directory = options["store"] / prepared["plan_id"]
    manifest = read_json(directory / "manifest.json")
    # Reproduce an artifact accepted before the profile-specific source guard:
    # its source record and identity agree, but omit the optional module hash.
    del manifest["sources"]["files_sha256"]["forgeflow/planning/retention.py"]
    atomic_json(options["bundle"] / "provenance.json", manifest["sources"], replace=True)
    atomic_json(directory / "manifest.json", manifest, replace=True)
    plan_id = digest(manifest)
    directory.rename(options["store"] / plan_id)
    with pytest.raises(service.RecoveryError, match="incomplete_source_manifest"):
        service.apply(connection, plan_id, **options)
    assert connection.writes == []


@pytest.mark.parametrize("tamper", ["source_hash", "source_plan", "profile", "missing", "orphan"])
def test_service_rejects_unbound_or_corrupt_refinement_audit(system, tamper):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, retention_profile=retention.RETENTION_PROFILE,
                               **options)
    plan = artifact(options, prepared, "plan")
    profile = retention.RETENTION_PROFILE
    if tamper == "source_hash":
        plan["retention_refinement"]["original_plan_sha256"] = "0" * 64
    elif tamper == "source_plan":
        plan["retention_refinement"]["original_plan"]["plan"]["expected_new_spend"] += 1
    elif tamper == "profile":
        plan["retention_refinement"]["profile"] = "other-profile"
    elif tamper == "missing":
        del plan["retention_refinement"]
    else:
        profile = None
    with pytest.raises(service.RecoveryError, match="retention_profile_binding_changed"):
        service._check_retention_binding(profile, plan)


def test_tampering_embedded_baseline_cannot_execute_with_prior_plan_identity(system):
    connection, request, options, _ = system
    prepared = service.prepare(connection, request, retention_profile=retention.RETENTION_PROFILE,
                               **options)
    plan = artifact(options, prepared, "plan")
    plan["retention_refinement"]["original_plan"]["plan"]["expected_new_spend"] += 1
    atomic_json(options["store"] / prepared["plan_id"] / "plan.json", plan, replace=True)
    with pytest.raises(service.RecoveryError, match="original_plan_artifact_changed"):
        service.apply(connection, prepared["plan_id"], **options)
    assert not connection.writes


def test_profile_survives_local_product_revision_normalization_and_approval(wired, monkeypatch):
    cases, case_id, executor, connection, options, verifications, root = wired
    root.retention_profile = retention.RETENTION_PROFILE
    executor.supports_revisions = True
    # This fixture models ERP writes around its synthetic schedule. It has no
    # optimality certificate, so refinement correctly skips while remaining bound.
    bind_synthetic_solver(monkeypatch)
    first = cases.prepare(case_id, local_tests.ACTOR)
    assert first["state"] == "awaiting_approval"
    summary = first["plan"]["retention_refinement"]
    assert summary["profile"] == retention.RETENTION_PROFILE and summary["status"] == "skipped"
    assert summary["reason"] == "original_optima_not_proven"
    assert set(summary) == {"profile", "status", "reason", "retained_before", "retained_after"}
    assert "original_plan" not in json.dumps(first)
    revised = cases.request_revision(case_id, local_tests.ACTOR, first["plan"]["plan_id"],
        reason="Raise the independently checked margin floor", changes={"minimum_margin_pct": 90})
    assert revised["state"] == "awaiting_approval", revised
    assert revised["revision"]["version"] == 2
    assert revised["plan"]["retention_refinement"] == summary
    old_id, new_id = first["plan"]["plan_id"], revised["plan"]["plan_id"]
    assert old_id != new_id
    assert not connection.writes
    stored = cases._read(case_id)
    intent = stored.preparation["intent"]
    manifest = read_json(options["store"] / new_id / "manifest.json")
    assert intent["inputs_sha256"] == manifest["product"]["inputs_sha256"]
    assert manifest["retention_profile"] == retention.RETENTION_PROFILE
    restarted = ProductCaseService(cases.root.parent, executor)
    assert restarted.get(case_id, local_tests.ACTOR) == revised
    with pytest.raises(ProductCaseError):
        restarted.approve(case_id, local_tests.ACTOR, old_id)
    assert not connection.writes
    approved = restarted.approve(case_id, local_tests.ACTOR, new_id)
    assert approved["state"] == "succeeded", approved
    assert approved["plan"]["retention_refinement"] == summary
    writes = list(connection.writes)
    assert writes and len(verifications) == 1
    assert restarted.approve(case_id, local_tests.ACTOR, new_id) == approved
    assert connection.writes == writes


def test_candidate_profile_survives_comparison_selection_and_margin_revision(case_service, monkeypatch):
    cases, case_id, executor, system, _, _, root = case_service
    root.retention_profile = retention.RETENTION_PROFILE
    first = cases.prepare(case_id, candidate_case_tests.ACTOR)
    assert first["state"] == "awaiting_selection"
    summaries = [plan["retention_refinement"] for plan in first["comparison"]["plans"]]
    assert [value["status"] for value in summaries] == ["unchanged", "skipped"]
    assert all(value["profile"] == retention.RETENTION_PROFILE for value in summaries)
    assert "original_plan" not in json.dumps(first)
    selected = candidate_case_tests.choose(cases, case_id, first, index=0)
    assert selected["plan"]["retention_refinement"] == summaries[0]
    monkeypatch.setattr(service, "solve_blueprint", solve_blueprint)
    revised = cases.request_revision(case_id, candidate_case_tests.ACTOR, selected["plan"]["plan_id"],
        reason="Keep the profile while tightening margin", changes={"minimum_margin_pct": 90})
    assert revised["state"] == "awaiting_selection", revised
    assert revised["revision"]["version"] == 2
    assert [plan["retention_refinement"] for plan in revised["comparison"]["plans"]] == summaries
    chosen = candidate_case_tests.choose(cases, case_id, revised, index=0)
    assert chosen["plan"]["retention_refinement"] == summaries[0]
    assert ProductCaseService(cases.root.parent, executor).get(case_id, candidate_case_tests.ACTOR) == chosen
    assert not system[0].writes

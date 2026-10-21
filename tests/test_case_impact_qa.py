"""Read-only impact answers retain source scope, selected identity and lifecycle facts."""
from __future__ import annotations

import copy
import json

import pytest
from test_product_impact import built, two_order_inputs

from forgeflow.api.case_context import case_context
from forgeflow.api.case_qa import _get, answer_case
from forgeflow.api.evidence import public_evidence
from forgeflow.api.impact_context import checked_chain
from forgeflow.product.executor import build_fake_plan
from forgeflow.product.state import AWAITING_APPROVAL, ProductCase, normalize_plan


def impact_case():
    chain, _, _, readiness, keys = built(two_order_inputs())
    case = ProductCase.new("owner", "A")
    plan = {**build_fake_plan(case.view()), **keys, "impact_chain": chain, "order_readiness": readiness}
    case.plan = normalize_plan(case, plan)
    case.state = AWAITING_APPROVAL
    return case


@pytest.mark.parametrize("question", ["Explain the supply path for SO2", "说明 SO2 的影响链"])
def test_specific_order_path_survives_public_redaction_with_scalar_evidence(question):
    case = impact_case().view()
    public = public_evidence(case)
    assert public["plan"]["impact_chain"] == case["plan"]["impact_chain"]
    before = copy.deepcopy(public)
    reply = answer_case(question, public)
    assert "SO2" in reply["answer"] and "SO1" not in reply["answer"]
    assert "4 units of FIN" in reply["answer"] or "4 件 FIN" in reply["answer"]
    assert "whole manufacturing order" in reply["answer"] or "属于整张制造单" in reply["answer"]
    assert "not proof of receipt" in reply["answer"] or "不证明已收货" in reply["answer"]
    assert reply["citations"] and all(_get(public, row["field"]) == row["value"] for row in reply["citations"])
    assert any("edges." in row["field"] and row["field"].endswith(".quantity") for row in reply["citations"])
    assert any(row["value"] == "recorded_scope_quantity_unknown" for row in reply["citations"])
    assert public == before


@pytest.mark.parametrize("question", ["Which purchase orders supply SO999?", "SO999 的供应路径是什么？"])
def test_unknown_order_does_not_receive_another_orders_path(question):
    reply = answer_case(question, impact_case().view())
    assert "SO1" not in reply["answer"] and "SO2" not in reply["answer"]
    assert "cannot substitute" in reply["answer"] or "不能拿其他订单代替" in reply["answer"]
    assert not any("impact_chain.edges" in row["field"] for row in reply["citations"])


@pytest.mark.parametrize("fault", ["legacy", "parent", "source_extra", "node_extra", "missing_edge", "null"])
def test_missing_or_invalid_chain_stays_unknown_and_does_not_leak_private_fields(fault):
    case = impact_case().view()
    plan = case["plan"]
    if fault == "legacy":
        plan.pop("impact_chain")
    elif fault == "null":
        plan["impact_chain"] = None
    elif fault == "parent":
        plan["plan_id"] = "d" * 64
    elif fault == "source_extra":
        plan["impact_chain"]["source_facts"]["secret"] = "CANARY_PRIVATE"
    elif fault == "node_extra":
        plan["impact_chain"]["nodes"][0]["secret"] = "CANARY_PRIVATE"
    else:
        plan["impact_chain"]["edges"].pop()
    reply = answer_case("Explain SO1 supply path", case)
    assert "no verifiable supply-relationship record" in reply["answer"]
    assert "CANARY_PRIVATE" not in json.dumps(reply)
    assert not any("impact_chain" in row["field"] for row in reply["citations"])
    context = json.loads(case_context(case))
    assert "CANARY_PRIVATE" not in json.dumps(context)
    if fault not in {"legacy", "null"}:
        assert "invalid" in context["plan"]["impact_chain_summary"]["status"]


@pytest.mark.parametrize("pending", ["selection_pending", "revision_pending", "repreparation_pending"])
def test_unconfirmed_selected_version_precedes_supply_graph(pending):
    case = impact_case().view()
    case["decision"][pending] = True
    reply = answer_case("Explain SO2 supply path and execution", case)
    assert "confirm" in reply["answer"].lower()
    assert "SO2" not in reply["answer"]
    assert not any("impact_chain" in row["field"] for row in reply["citations"])


def test_mixed_execution_answer_keeps_unknown_writes_and_only_planned_chain():
    case = impact_case().view()
    case.update(state="execution_unknown", execution={"status": "unknown", "writes": [], "verifier": {}})
    reply = answer_case("Explain SO2 supply path and execution result", case)
    assert "writes may already have occurred" in reply["answer"] and "do not approve it again" in reply["answer"]
    assert "4 units of FIN" in reply["answer"] and "not proof of receipt" in reply["answer"]
    assert "0 writes" not in reply["answer"]


def test_context_omits_graph_without_losing_candidate_and_execution_decision_facts():
    case = impact_case().view()
    plans = []
    for i, objective in enumerate(("preserve_quantities", "min_planned_cost", "preserve_capacity")):
        plan = copy.deepcopy(case["plan"])
        plan["objective"] = objective
        plan["actions"] = [{"summary": "document " * 100}] * 100
        plan["costs"] = {"planned": {"total": str(70 + i)}, "basis": {"currency": "USD"}}
        plans.append(plan)
    case["plan"] = plans[1]
    case["comparison"] = {"status": "ready", "plans": plans, "selected_plan_id": plans[1]["plan_id"]}
    case["execution"] = {"status": "unknown", "verifier": {"status": "not_run"}}
    encoded = case_context(case)
    context = json.loads(encoded)
    assert len(encoded) <= 6000
    assert "source_facts" not in encoded and '"impact_chain"' not in encoded
    assert "detailed_impact_chain" in context["omitted_sections"]
    assert [plan["costs"]["planned"]["total"] for plan in context["comparison"]["plans"]] == ["70", "71", "72"]
    assert context["execution"]["status"] == "unknown"
    assert checked_chain(case["plan"]) is not None


@pytest.mark.parametrize("target", ["current", "history"])
@pytest.mark.parametrize("fault", ["parent", "readiness", "edge"])
def test_saved_current_and_retired_graphs_reject_identity_or_relationship_mutation(target, fault):
    case = impact_case()
    case.history = [{"version": 1, "plan": copy.deepcopy(case.plan)}]
    original = case.to_record()
    assert ProductCase.from_record(original).view()["plan"]["impact_chain"] == case.plan["impact_chain"]
    record = copy.deepcopy(original)
    plan = record["plan"] if target == "current" else record["history"][0]["plan"]
    if fault == "parent":
        plan["plan_id"] = "b" * 64
    elif fault == "readiness":
        plan["order_readiness"]["orders"][0]["buffer_days"] += 1
    else:
        plan["impact_chain"]["edges"][0]["quantity"] = 1
    with pytest.raises(ValueError):
        ProductCase.from_record(record)
    if target == "current":
        case.plan = plan
    else:
        case.history[0]["plan"] = plan
    for project in (case.to_record, case.view):
        with pytest.raises(ValueError):
            project()

"""Shared grading uses real fixture prepare and hostile artifact/trace edits."""

import copy
import importlib.util
import json
import shutil
from pathlib import Path

import pytest

from forgeflow.investigation.fixtures import development_cases
from forgeflow.investigation.grading import grade_clarification, grade_episode
from forgeflow.investigation.rules import run_rules
from forgeflow.recovery.storage import digest, read_json


@pytest.fixture(scope="module")
def runner():
    path = Path(__file__).resolve().parents[1] / "scripts/investigation_evaluate.py"
    spec = importlib.util.spec_from_file_location("fixture_grading_runner", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def baseline_request(episode):
    """A test control reads the public query API and submits a normal old request."""
    def query(kind, **arguments):
        return episode.dispatch("query", {"kind": kind, "arguments": arguments})["records"]

    purchase = query("entities", entity="purchase_order", query="PO-CANCEL")[0]
    supplier = query("record", entity="supplier", id=purchase["fields"]["supplier_id"])[0]
    line = next(row for row in query("relations", entity="purchase_order", id=purchase["id"])
                if row["entity"] == "purchase_order_line")
    product = query("record", entity="product", id=line["fields"]["product_id"])[0]
    relations = query("relations", entity="product", id=product["id"])
    orders = {}
    for row in relations:
        if row["entity"] != "sales_order_line":
            continue
        order = query("record", entity="sales_order", id=row["fields"]["order_id"])[0]
        if order["fields"]["state"] == "sale" and row["fields"]["quantity"] > row["fields"]["delivered_quantity"]:
            orders[int(order["id"])] = order
    policy = query("policy")[0]
    budgets = {str(identity): query("policy", order_id=identity)[0]["fields"]["budget_cap"] for identity in orders}
    offer = next(row for row in relations if row["entity"] == "supplier_offer"
                 and row["fields"]["supplier_id"] == supplier["id"]
                 and row["fields"]["unit_price"] == line["fields"]["unit_price"])
    return {"target_product_code": product["fields"]["default_code"], "sales_order_ids": sorted(orders),
            "anchor_date": policy["fields"]["anchor_date"], "margin": policy["fields"]["margin"],
            "sales_order_budget_caps": budgets, "disruption": {"kind": policy["fields"]["task_type"],
                "vendor_ref": supplier["fields"]["ref"], "offer_key": offer["fields"]["offer_key"]}}


@pytest.fixture(scope="module")
def episodes(runner, tmp_path_factory):
    root = tmp_path_factory.mktemp("shared-business-grading")
    ready = {}
    for index, case in enumerate(development_cases()):
        path = root / f"rules-{index}"
        episode = runner.Episode(case, "rules", path, 600, 64000)
        run_rules(case.event, episode.dispatch)
        episode.report()
        ready[("rules", index)] = path
    for index in (0, 2, 6, 7):
        case = development_cases()[index]
        path = root / f"baseline-{index}"
        episode = runner.Episode(case, "baseline", path, 600, 64000)
        episode.dispatch("prepare", baseline_request(episode))
        episode.report()
        ready[("baseline", index)] = path
    return ready


def clone(episodes, tmp_path, arm="baseline", index=0):
    return Path(shutil.copytree(episodes[(arm, index)], tmp_path / "episode"))


def save(path, value):
    path.write_text(json.dumps(value) + "\n")


@pytest.mark.parametrize("index", range(8))
def test_each_real_rule_outcome_has_required_business_proof(episodes, index):
    graded = grade_episode(development_cases()[index], episodes[("rules", index)])
    assert graded["score"] == 1, graded
    assert graded["denominator"] == 1
    assert graded["request_denominator"] == int(index in {0, 1, 2, 3, 6})
    assert graded["grader_reads_excluded_from_agent_cost"] is True


@pytest.mark.parametrize("index", [0, 2, 6, 7])
def test_baseline_is_graded_without_new_draft_or_review_protocol(episodes, index):
    graded = grade_episode(development_cases()[index], episodes[("baseline", index)])
    assert graded["score"] == 1, graded
    assert graded["protocol_integrity"] is None
    assert graded["source_coverage"] is True


def test_correct_request_and_real_plan_do_not_credit_host_reads_as_agent_evidence(episodes, tmp_path):
    root = clone(episodes, tmp_path)
    trace = [row for row in read_json(root / "trace.json") if row["action"] != "query"]
    save(root / "trace.json", trace)
    graded = grade_episode(development_cases()[0], root)
    assert graded["request_correct"] is True
    assert graded["artifact_integrity"] is True
    assert graded["source_coverage"] is False
    assert graded["coverage_details"]["agent_records"] == 0
    assert graded["grader_queries"] > 0
    assert graded["score"] == 0


@pytest.mark.parametrize("after_prepare", [False, True])
def test_budget_must_be_read_before_submission(episodes, tmp_path, after_prepare):
    root = clone(episodes, tmp_path)
    trace = read_json(root / "trace.json")
    budget = [row for row in trace if row["action"] == "query" and row["payload"]["kind"] == "policy"
              and "order_id" in row["payload"]["arguments"]]
    trace = [row for row in trace if row not in budget]
    if after_prepare:
        trace.extend(budget)
    save(root / "trace.json", trace)
    graded = grade_episode(development_cases()[0], root)
    assert graded["request_correct"] is True
    assert graded["source_coverage"] is False
    assert "sales_order_budget_caps.1" in graded["coverage_details"]["missing_fields"]
    assert graded["score"] == 0


def test_forged_query_response_does_not_create_usable_observation(episodes, tmp_path):
    root = clone(episodes, tmp_path)
    trace = read_json(root / "trace.json")
    row = next(row for row in trace if row["action"] == "query" and row["payload"]["kind"] == "policy"
               and "order_id" in row["payload"]["arguments"])
    row["result"]["records"][0]["fields"]["budget_cap"] = 999
    save(root / "trace.json", trace)
    graded = grade_episode(development_cases()[0], root)
    assert graded["query_trace_authentic"] is False
    assert graded["score"] == 0


@pytest.mark.parametrize("name", ["request", "snapshot", "blueprint", "plan", "change-set", "baseline", "binding"])
def test_every_prepared_artifact_is_bound(episodes, tmp_path, name):
    root = clone(episodes, tmp_path)
    path = next((root / "trusted/store").glob("*/manifest.json")).parent / (name + ".json")
    value = read_json(path)
    value["injected"] = True
    save(path, value)
    assert grade_episode(development_cases()[0], root)["score"] == 0


def test_manifest_digest_is_the_plan_identity(episodes, tmp_path):
    root = clone(episodes, tmp_path)
    path = next((root / "trusted/store").glob("*/manifest.json"))
    value = read_json(path)
    value["schema_version"] = 99
    save(path, value)
    assert grade_episode(development_cases()[0], root)["score"] == 0


def test_incomplete_prepare_without_completion_marker_is_not_prepared(episodes, tmp_path):
    root = clone(episodes, tmp_path)
    next((root / "trusted/store").glob("*/prepared.json")).unlink()
    assert grade_episode(development_cases()[0], root)["score"] == 0


def test_wrong_offer_on_same_supplier_is_a_wrong_request(episodes, tmp_path):
    root = clone(episodes, tmp_path, index=2)
    trace = read_json(root / "trace.json")
    next(row for row in trace if row["action"] == "prepare")["payload"]["disruption"]["offer_key"] = "V-OLD|FIN|1|55.0|1"
    save(root / "trace.json", trace)
    graded = grade_episode(development_cases()[2], root)
    assert graded["request_correct"] is False
    assert graded["score"] == 0


def test_rehashing_invalid_execution_binding_does_not_make_a_valid_artifact(episodes, tmp_path):
    root = clone(episodes, tmp_path)
    manifest_path = next((root / "trusted/store").glob("*/manifest.json"))
    directory = manifest_path.parent
    binding = read_json(directory / "binding.json")
    binding["steps_sha256"] = "0" * 64
    save(directory / "binding.json", binding)
    manifest = read_json(manifest_path)
    manifest["artifacts"]["binding"] = digest(binding)
    save(manifest_path, manifest)
    plan_id = digest(manifest)
    prepared = read_json(directory / "prepared.json")
    prepared["plan_id"] = plan_id
    save(directory / "prepared.json", prepared)
    directory.rename(directory.with_name(plan_id))
    summary = read_json(root / "summary.json")
    summary["terminal"]["plan_id"] = plan_id
    trace = read_json(root / "trace.json")
    for row in trace:
        if row["action"] == "prepare":
            row["result"]["plan_id"] = plan_id
    save(root / "trace.json", trace)
    assert grade_episode(development_cases()[0], root, summary=summary)["score"] == 0


@pytest.mark.parametrize("mutation", ["review", "draft", "state_observation"])
def test_new_protocol_hashes_are_an_additional_gate(episodes, tmp_path, mutation):
    root = clone(episodes, tmp_path, "rules")
    directory = root / "trusted/store/investigation"
    name = "review.json" if mutation == "review" else "state.json"
    value = read_json(directory / name)
    if mutation == "review":
        value["request"]["sales_order_budget_caps"]["1"] = 999
    elif mutation == "draft":
        value["drafts"][-1]["draft"]["reason"] = "changed after validation"
    else:
        key = value["drafts"][-1]["draft"]["supply_observation_id"]
        value["observations"][key]["record"]["fields"]["origin"] = "OTHER"
    save(directory / name, value)
    graded = grade_episode(development_cases()[0], root)
    assert graded["score"] == 0


@pytest.mark.parametrize("mutation", ["missing", "hash", "blueprint", "feasible_trace"])
def test_infeasible_requires_true_bound_solver_failure(episodes, tmp_path, mutation):
    root = clone(episodes, tmp_path, index=6)
    proof = next((root / "trusted").glob("solver-failure-*.json"))
    if mutation == "missing":
        proof.unlink()
    else:
        value = read_json(proof)
        if mutation == "hash":
            value["solver_proof_sha256"] = "0" * 64
        elif mutation == "blueprint":
            value["solver_proof"]["blueprint"]["margin"] = 0.9
            value["solver_proof_sha256"] = digest(value["solver_proof"])
        else:
            value["solver_proof"]["trace"]["solve_calls"][-1]["status"] = "FEASIBLE"
            value["solver_proof_sha256"] = digest(value["solver_proof"])
        save(proof, value)
    graded = grade_episode(development_cases()[6], root)
    assert graded["request_correct"] is True
    assert graded["terminal_proof"] is False
    assert graded["score"] == 0


def test_self_declared_unsupported_does_not_count_as_real_scope_failure(episodes, tmp_path):
    root = clone(episodes, tmp_path, index=7)
    trace = read_json(root / "trace.json")
    for row in trace:
        if row["action"] == "prepare":
            row["action"] = "finish"
    save(root / "trace.json", trace)
    assert grade_episode(development_cases()[7], root)["score"] == 0


@pytest.mark.parametrize("field,value", [("business_unchanged", False), ("write_attempts", [{"method": "write"}]),
                                         ("host_source_unchanged", False)])
def test_integrity_failure_stays_in_denominator(episodes, tmp_path, field, value):
    root = clone(episodes, tmp_path)
    summary = read_json(root / "summary.json")
    summary[field] = value
    graded = grade_episode(development_cases()[0], root, summary=summary)
    assert graded["score"] == 0 and graded["denominator"] == graded["request_denominator"] == 1


@pytest.mark.parametrize("status", ["budget_exhausted", "technical_failure"])
def test_timeouts_and_technical_errors_are_not_removed(episodes, tmp_path, status):
    root = clone(episodes, tmp_path)
    summary = read_json(root / "summary.json")
    summary["status"] = status
    summary["terminal"] = {"status": status, "reason": "time_limit"}
    graded = grade_episode(development_cases()[0], root, summary=summary)
    assert graded["score"] == 0 and graded["denominator"] == 1


@pytest.mark.parametrize("question,verdict", [
    ("What budget cap is authorized for SO-1?", "correct"),
    ("请提供 SO-1 的预算上限。", "correct"),
    ("What budget cap is authorized for sales order ID 1?", "correct"),
    ("Please provide the budget for sales order ID: 1.", "correct"),
    ("What is the budget for sales order #1?", "correct"),
    ("请提供销售订单ID1的预算上限。", "correct"),
    ("销售订单编号：1的预算是多少？", "correct"),
    ("What budget applies to SO-1 (sales order ID 1)?", "correct"),
    ("Please provide more information.", "incorrect"),
    ("What budget applies to SO-2?", "incorrect"),
    ("What is the budget for order 1?", "incorrect"),
    ("What budget applies to sales order ID 11?", "incorrect"),
    ("What budget applies to purchase order ID 1?", "incorrect"),
    ("What budget applies to sales order ID 1.5?", "incorrect"),
    ("Please confirm sales order ID 1 budget 2000?", "incorrect"),
    ("Please confirm sales order ID 1 budget 1 dollar?", "incorrect"),
    ("请确认销售订单ID1的预算是2000？", "incorrect"),
    ("Please confirm sales order ID 1 budget 1.50?", "incorrect"),
    ("SO-1 budget is 2000. What next?", "incorrect"),
    ("Please confirm SO-1 budget 2000?", "incorrect"),
    ("Please do not provide SO-1 budget.", "incorrect"),
    ("What budget cap is authorized for SO-1? Also which supplier should we use?", "needs_manual_review"),
    ("SO-1 needs its spending envelope settled.", "needs_manual_review"),
])
def test_budget_questions_require_exact_business_identity_and_missing_field(question, verdict):
    assert grade_clarification(development_cases()[4], question)["verdict"] == verdict


@pytest.mark.parametrize("question,verdict", [
    ("Does the cancellation concern PO-CANCEL or PO-OTHER?", "correct"),
    ("Which purchase order did Fixture old vendor cancel?", "correct"),
    ("Which PO was canceled?", "incorrect"),
    ("Which supplier canceled the commitment: V-OLD or V-NEW?", "correct"),
    ("取消的是供应商 V-OLD 还是 V-NEW 的承诺？", "correct"),
    ("Which purchase order did V-OLD cancel?", "needs_manual_review"),
    ("Could you clarify the disruption?", "incorrect"),
    ("PO-CANCEL seems questionable.", "needs_manual_review"),
])
def test_event_questions_must_identify_real_candidates_or_supplier(question, verdict):
    assert grade_clarification(development_cases()[5], question)["verdict"] == verdict


@pytest.mark.parametrize("mutation", ["multiple_orders", "duplicate_ref", "third_candidate"])
def test_supplier_references_only_auto_resolve_a_bijection_to_all_candidate_orders(mutation):
    case = development_cases()[5]
    records = case.records
    if mutation == "duplicate_ref":
        duplicate = copy.deepcopy(records["res.partner"][2])
        duplicate.update(id=99, name="Another supplier")
        records["res.partner"].append(duplicate)
    else:
        po = copy.deepcopy(records["purchase.order"][0])
        po.update(id=99, name="PO-THIRD")
        line = copy.deepcopy(records["purchase.order.line"][0])
        line.update(id=99, order_id=99)
        if mutation == "third_candidate":
            supplier = copy.deepcopy(records["res.partner"][2])
            supplier.update(id=99, ref="V-THIRD")
            records["res.partner"].append(supplier)
            po["partner_id"] = 99
        records["purchase.order"].append(po)
        records["purchase.order.line"].append(line)
    graded = grade_clarification(case, "Which supplier canceled the commitment: V-OLD or V-NEW?")
    assert graded["verdict"] == "needs_manual_review"


def test_manual_review_is_not_automatically_a_pass(episodes, tmp_path):
    root = clone(episodes, tmp_path, "rules", 4)
    summary = read_json(root / "summary.json")
    question = "SO-1 needs its spending envelope settled."
    summary["terminal"]["terminal"]["question"] = question
    state_path = root / "trusted/store/investigation/state.json"
    state = read_json(state_path)
    state["terminal"]["question"] = question
    save(state_path, state)
    trace = read_json(root / "trace.json")
    for row in trace:
        if row["action"] == "finish":
            row["payload"]["question"] = question
            row["result"]["terminal"]["question"] = question
    save(root / "trace.json", trace)
    graded = grade_episode(development_cases()[4], root, summary=summary)
    assert graded["score"] is None
    assert graded["needs_manual_review"] is True
    assert graded["denominator"] == 1 and graded["request_denominator"] == 0


def test_clarification_on_a_solvable_case_is_not_rewarded(episodes, tmp_path):
    root = clone(episodes, tmp_path)
    summary = copy.deepcopy(read_json(root / "summary.json"))
    summary.update(status="needs_clarification", terminal={"status": "needs_clarification",
                                                          "question": "What budget applies to SO-1?"})
    graded = grade_episode(development_cases()[0], root, summary=summary)
    assert graded["score"] == 0 and graded["denominator"] == 1

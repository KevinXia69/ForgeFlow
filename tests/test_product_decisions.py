"""Product decisions against synthetic executors and temporary stores only."""

from __future__ import annotations

import copy
import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from forgeflow.product.executor import FakeProductExecutor, ProductExecutionError
from forgeflow.product.service import ProductCaseError, ProductCaseService
from forgeflow.product.state import execution_result


def prepared(tmp_path, executor=None):
    executor = executor or FakeProductExecutor()
    service = ProductCaseService(tmp_path, executor)
    case = service.create("owner-a", "A")
    return service, executor, service.prepare(case["case_id"], "owner-a")


def test_synthetic_demand_and_actions_match_the_case_quantity(tmp_path):
    service, executor, case = prepared(tmp_path)
    units = case["order"]["units"]
    assert units == 50
    check = next(row for row in case["plan"]["checks"] if row["code"] == "demand")
    assert check["actual"] == f"{units}/{units} units"
    assert check["threshold"] == f"{units} units"
    assert "synthetic" in check["source"]
    assert all(action["quantity"] == units for action in case["plan"]["actions"])
    assert all(action["before"] is None and action["after"] is None for action in case["plan"]["actions"])
    result = service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    assert all(record["quantity"] == units for record in result["execution"]["readback"]["records"])
    assert result["execution"]["verifier"]["source"] == "synthetic_executor"
    assert result["result"]["verified"] is False
    assert result["result"]["simulation_passed"] is True
    assert result["result"]["verification_source"] == "synthetic_executor"
    assert result["plan"]["new_spend"] == 420
    assert result["plan"]["cost_delta"] is None
    assert executor.apply_calls == 1


def test_recorded_action_snapshots_survive_reload_without_unapproved_fields(tmp_path):
    class SnapshotExecutor(FakeProductExecutor):
        def prepare(self, case):
            plan = super().prepare(case)
            plan["actions"][0].update(
                before={"quantity": 0, "state": "purchase", "api_key": "synthetic-do-not-export"},
                after={"state": "cancel", "origin_refs": [{"model": "sale.order", "name": "SO1", "secret": "synthetic-do-not-export"}]},
            )
            return plan
    service, _, case = prepared(tmp_path, SnapshotExecutor())
    action = case["plan"]["actions"][0]
    assert action["before"] == {"quantity": 0, "state": "purchase"}
    assert action["after"] == {"state": "cancel", "origin_refs": [{"model": "sale.order", "name": "SO1"}]}
    assert "synthetic-do-not-export" not in json.dumps(case)
    assert ProductCaseService(tmp_path).get(case["case_id"], "owner-a")["plan"]["actions"][0] == action


class CheckedExecutor(FakeProductExecutor):
    def __init__(self, code, status):
        super().__init__()
        self.code, self.status = code, status

    def prepare(self, case):
        plan = super().prepare(case)
        existing = next((row for row in plan["checks"] if row["code"] == self.code), None)
        if existing:
            existing["status"] = self.status
        else:
            plan["checks"].append({"code": self.code, "status": self.status})
        return plan


@pytest.mark.parametrize(("code", "status", "reason"), [
    ("margin", "block", "business_check_blocked"),
    ("margin", "warn", "hard_constraint_not_satisfied"),
    ("margin", "unknown", "required_checks_unknown"),
    ("budget_policy", "unknown", "required_checks_unknown"),
    ("demand", "unknown", "required_checks_unknown"),
    ("on_time", "unknown", "required_checks_unknown"),
    ("informational", "block", "business_check_blocked"),
])
def test_server_refuses_business_constraints_without_issuing_approval(tmp_path, code, status, reason):
    service, executor, case = prepared(tmp_path, CheckedExecutor(code, status))
    assert case["decision"]["can_approve"] is False
    assert case["decision"]["reason"] == reason
    with pytest.raises(ProductCaseError, match=reason):
        service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    assert executor.apply_calls == 0
    assert service.get(case["case_id"], "owner-a")["approval"] is None


@pytest.mark.parametrize("status", ["warn", "unknown"])
def test_informational_check_does_not_override_or_add_hard_constraints(tmp_path, status):
    service, executor, case = prepared(tmp_path, CheckedExecutor("measured_cost_delta", status))
    assert case["decision"]["can_approve"]
    service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    assert executor.apply_calls == 1


def test_rejects_prepared_plan_persistently_without_an_executor(tmp_path):
    service, executor, case = prepared(tmp_path)
    offline = ProductCaseService(tmp_path)
    rejected = offline.reject(case["case_id"], "owner-a", "Supplier confirmation missing", plan_id=case["plan"]["plan_id"])
    assert rejected["state"] == "rejected"
    assert rejected["audit"][-1]["comment"] == "Supplier confirmation missing"
    assert ProductCaseService(tmp_path).get(case["case_id"], "owner-a")["state"] == "rejected"
    with pytest.raises(ProductCaseError, match="case_rejected"):
        service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    assert executor.apply_calls == 0
    assert offline.reject(case["case_id"], "owner-a")["audit"] == rejected["audit"]


def test_comments_and_authenticated_actor_survive_reload_and_replay(tmp_path):
    service, executor, case = prepared(tmp_path)
    result = service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"], comment="Check supplier receipt")
    assert result["approval"]["approver"] == "owner-a"
    assert result["approval"]["comment"] == "Check supplier receipt"
    assert all("uid" not in row for row in result["audit"])
    stored = json.loads(service.path_for(case["case_id"]).read_text())
    assert any(row["uid"] == "owner-a" and row["action"] == "approved" for row in stored["audit"])
    replay = ProductCaseService(tmp_path).approve(case["case_id"], "owner-a", case["plan"]["plan_id"], comment="Cannot edit history")
    assert replay["approval"] == result["approval"]
    assert executor.apply_calls == 1


def test_revision_preserves_old_version_and_old_plan_cannot_be_approved(tmp_path):
    service, executor, case = prepared(tmp_path)
    old = copy.deepcopy(case["plan"])
    revised = service.request_revision(case["case_id"], "owner-a", old["plan_id"],
                                       reason="Raise margin floor", changes={"minimum_margin_pct": 30})
    assert revised["revision"]["version"] == 2
    assert revised["revision"]["parent_plan_id"] == old["plan_id"]
    assert revised["history"][0]["plan"] == old
    assert revised["plan"]["plan_id"] != old["plan_id"]
    assert revised["approval"] is None
    assert revised["state"] == "awaiting_approval"
    with pytest.raises(ProductCaseError, match="plan_mismatch"):
        service.approve(case["case_id"], "owner-a", old["plan_id"])
    service.approve(case["case_id"], "owner-a", revised["plan"]["plan_id"])
    assert executor.applied_plan_ids == [revised["plan"]["plan_id"]]
    with pytest.raises(ProductCaseError, match="revision_unsupported"):
        service.request_revision(case["case_id"], "owner-a", revised["plan"]["plan_id"], reason="Too late", changes={"minimum_margin_pct": 31})


@pytest.mark.parametrize("changes", [None, {}, {"ignore_policy": True}, {"minimum_margin_pct": 20}])
def test_unsupported_revision_changes_do_not_retire_the_plan(tmp_path, changes):
    service, executor, case = prepared(tmp_path)
    with pytest.raises(ProductCaseError):
        service.request_revision(case["case_id"], "owner-a", case["plan"]["plan_id"], reason="Change", changes=changes)
    assert service.get(case["case_id"], "owner-a")["plan"] == case["plan"]
    assert executor.prepare_calls == 1


def test_two_service_instances_cannot_dispatch_same_plan_twice(tmp_path):
    service, executor, case = prepared(tmp_path)
    another = ProductCaseService(tmp_path, executor)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda target: target.approve(case["case_id"], "owner-a", case["plan"]["plan_id"]), [service, another]))
    assert all(result["state"] == "succeeded" for result in results)
    assert executor.apply_calls == 1


class LostResponse(FakeProductExecutor):
    def apply(self, case, plan):
        self.recorded = super().apply(case, plan)
        raise ProductExecutionError("unavailable", outcome_unknown=True)

    def reconcile(self, case, plan):
        return self.recorded


def test_lost_response_stays_unknown_and_reconciliation_never_reapplies(tmp_path):
    service, executor, case = prepared(tmp_path, LostResponse())
    attempted = service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    assert attempted["state"] == "execution_unknown"
    assert attempted["result"]["outcome"] == "execution_unknown"
    assert not attempted["decision"]["can_approve"]
    service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    restored = service.reconcile(case["case_id"], "owner-a")
    assert restored["result"]["verified"] is False
    assert restored["result"]["simulation_passed"] is True
    assert executor.apply_calls == 1


@pytest.mark.parametrize("verifier, verified, outcome", [
    ({"status": "passed", "score": 100}, False, "verification_unknown"),
    ({"status": "passed", "strict_success": False, "evaluation_valid": True}, False, "verification_failed"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True, "failed_rule_count": 1}, False, "verification_failed"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True,
      "rules": {"by_dimension": {"constraint": [{"status": "PASS"}, {"status": "FAIL"}]}}}, False, "verification_failed"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True,
      "rules": {"by_dimension": {"constraint": [{"status": "PASS", "applicable": False}]}}}, False, "verification_unknown"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True, "rules": {}}, False, "verification_unknown"),
    ({"status": "passed", "evaluation_valid": True,
      "rules": {"by_dimension": {"constraint": [{"status": "PASS"}]}}}, False, "verification_unknown"),
    ({"status": "passed", "strict_success": True}, False, "verification_unknown"),
    ({"status": "not_run"}, False, "verification_unknown"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True}, False, "verification_unknown"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True,
      "source": "synthetic_executor"}, False, "simulation_passed"),
    ({"status": "passed", "strict_success": True, "evaluation_valid": True,
      "evaluation_scope": "product_original_verifier"}, True, "verified"),
])
def test_result_requires_strict_and_valid_evidence(verifier, verified, outcome):
    execution = {"status": "succeeded", "readback": {"execution_validated": True}, "verifier": verifier}
    original = copy.deepcopy(execution)
    result = execution_result(execution, "succeeded")
    assert result["verified"] is verified
    assert result["outcome"] == outcome
    assert execution == original


def test_owner_isolation_applies_to_new_decisions(tmp_path):
    service, _, case = prepared(tmp_path)
    for call in (
        lambda: service.reject(case["case_id"], "owner-b"),
        lambda: service.request_revision(case["case_id"], "owner-b", case["plan"]["plan_id"], reason="change"),
        lambda: service.reconcile(case["case_id"], "owner-b"),
    ):
        with pytest.raises(ProductCaseError, match="case_not_found"):
            call()


def test_oversized_comment_does_not_start_execution(tmp_path):
    service, executor, case = prepared(tmp_path)
    with pytest.raises(ProductCaseError, match="invalid_decision_comment"):
        service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"], comment="x" * 2001)
    assert executor.apply_calls == 0


def test_missing_plan_metrics_remain_null_and_do_not_become_false_claims(tmp_path):
    class MinimalExecutor:
        def prepare(self, case):
            return {"plan_id": "a" * 64, "actions": [{"kind": "update_po", "document": "PO1"}]}

    _, _, case = prepared(tmp_path, MinimalExecutor())
    assert case["plan"]["margin"] is None
    assert case["plan"]["on_time"] is None
    assert case["plan"]["cost_delta"] is None
    assert case["plan"]["new_spend"] is None
    assert case["plan"]["actions"][0]["quantity"] is None
    assert case["decision"]["reason"] == "required_checks_unknown"


def test_unsupported_executor_cannot_offer_a_revision_that_would_ignore_changes(tmp_path):
    class OriginalOnly:
        def prepare(self, case):
            return FakeProductExecutor().prepare(case)

    service, _, case = prepared(tmp_path, OriginalOnly())
    assert case["decision"]["can_request_changes"] is False
    assert case["decision"]["revision_reason"] == "revision_unsupported"
    with pytest.raises(ProductCaseError, match="revision_unsupported"):
        service.request_revision(case["case_id"], "owner-a", case["plan"]["plan_id"], reason="Change", changes={"minimum_margin_pct": 30})
    assert service.get(case["case_id"], "owner-a")["history"] == []


def test_revision_preparation_failure_keeps_old_plan_retired_and_auditable(tmp_path):
    service, executor, case = prepared(tmp_path)
    executor.fail_prepare = True
    revised = service.request_revision(case["case_id"], "owner-a", case["plan"]["plan_id"], reason="Tighten", changes={"minimum_margin_pct": 30})
    assert revised["state"] == "failed"
    assert revised["plan"] is None
    assert revised["history"][0]["plan"] == case["plan"]
    with pytest.raises(ProductCaseError, match="plan_not_prepared"):
        service.approve(case["case_id"], "owner-a", case["plan"]["plan_id"])
    assert executor.apply_calls == 0


def test_reads_observe_executing_while_a_slow_apply_holds_the_mutation_lock(tmp_path):
    entered = threading.Event()
    release = threading.Event()

    class SlowExecutor(FakeProductExecutor):
        def apply(self, case, plan):
            entered.set()
            assert release.wait(5), "test did not release executor"
            return super().apply(case, plan)

    service, executor, case = prepared(tmp_path, SlowExecutor())
    with ThreadPoolExecutor(max_workers=3) as pool:
        applied = pool.submit(service.approve, case["case_id"], "owner-a", case["plan"]["plan_id"])
        try:
            assert entered.wait(2)
            observed = pool.submit(service.get, case["case_id"], "owner-a").result(timeout=1)
            listed = pool.submit(service.list_for, "owner-a").result(timeout=1)
            assert observed["state"] == "executing"
            assert listed[0]["state"] == "executing"
            assert observed["decision"]["can_approve"] is False
        finally:
            release.set()
        assert applied.result(timeout=2)["state"] == "succeeded"
    assert executor.apply_calls == 1

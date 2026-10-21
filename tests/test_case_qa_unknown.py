"""Unknown dispatch receipts are not evidence of zero writes; no ERP/model calls."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from forgeflow.api.app import create_app
from forgeflow.api.case_qa import answer_case
from forgeflow.api.settings import create_settings
from forgeflow.product import FakeProductExecutor
from forgeflow.product.executor import ProductExecutionError


def assert_unknown(reply, *, chinese=False):
    answer = reply["answer"]
    assert ("可能已经发生写入" if chinese else "writes may already have occurred") in answer
    assert ("勿重复批准" if chinese else "do not approve it again") in answer
    for phrase in ("finished", "执行结束", "0 writes", "0 readback", "0 项", "0 条"):
        assert phrase not in answer
    assert "execution.writes" not in {row["field"] for row in reply["citations"]}
    assert all(row["value"] != 0 for row in reply["citations"])


@pytest.mark.parametrize("question", ["What happened during execution?", "执行结果是什么？"])
@pytest.mark.parametrize("execution", [
    None,
    {"status": "unknown", "writes": [], "readback": None, "verifier": {}},
    {"status": "unknown", "writes": [{"document": "P00006", "kind": "cancel_po"}],
     "readback": {"records": []}, "verifier": {"status": "not_run"}},
])
def test_unknown_receipts_do_not_claim_completion_or_zero_writes(question, execution):
    case = {"state": "execution_unknown", "execution": execution, "reason": "postflight_failed"}
    reply = answer_case(question, case)
    assert_unknown(reply, chinese=question.startswith("执行"))
    assert "postflight_failed" in reply["answer"]


@pytest.mark.parametrize("projection", [
    {"state": "failed", "execution": {"status": "unknown", "writes": []}},
    {"state": "failed", "result": {"outcome": "execution_unknown"}},
])
def test_unknown_projection_takes_precedence_over_generic_failure(projection):
    assert_unknown(answer_case("Did execution finish?", projection))


@pytest.mark.parametrize("question,missing,zero", [
    ("What happened during execution?", "Readback record details were not recorded", "0 readback records"),
    ("执行结果是什么？", "回读明细未记录", "回读 0 条记录"),
])
def test_successful_readback_without_details_does_not_invent_zero_count(question, missing, zero):
    execution = {
        "status": "succeeded", "writes": [],
        "readback": {"execution_validated": True},
        "verifier": {"status": "passed", "strict_success": True, "evaluation_valid": True,
                     "evaluation_scope": "product_original_verifier"},
    }
    case = {"state": "succeeded", "execution": execution}
    answer = answer_case(question, case)["answer"]
    assert missing in answer
    assert zero not in answer
    execution["readback"]["records"] = []
    answer = answer_case(question, case)["answer"]
    assert zero in answer
    assert missing not in answer


def test_authenticated_case_qa_preserves_unknown_dispatch_and_never_reapplies(tmp_path):
    class LostReceipt(FakeProductExecutor):
        def apply(self, *args, **kwargs):
            self.apply_calls += 1
            raise ProductExecutionError("postflight_failed", outcome_unknown=True)

    settings = create_settings({
        "FORGEFLOW_DATA_ROOT": str(tmp_path / "evidence"),
        "FORGEFLOW_APPROVALS_ROOT": str(tmp_path / "approvals"),
        "FORGEFLOW_SESSION_SECRET": "isolated-unknown-qa-test-secret-0001",
        "FORGEFLOW_INVITE_CODES": "isolated-unknown-qa-test",
        "FORGEFLOW_SECURE_COOKIE": "true",
        "FORGEFLOW_DEPLOYMENT": "local",
    })
    executor = LostReceipt()
    with TestClient(create_app(settings, product_executor=executor), base_url="https://testserver") as client:
        assert client.post("/api/auth/session", json={"invite_code": "isolated-unknown-qa-test"}).status_code == 201
        created = client.post("/api/product/cases", json={"scenario": "A"}).json()
        path = f"/api/product/cases/{created['case_id']}"
        approved = client.post(path + "/approve", json={"plan_id": created["plan"]["plan_id"]})
        assert approved.status_code == 200
        assert approved.json()["state"] == "execution_unknown"
        before = client.get(path).json()
        for question in ("What happened during execution?", "执行结果是什么？"):
            reply = client.post(path + "/ask", json={"question": question})
            assert reply.status_code == 200
            assert_unknown(reply.json(), chinese=question.startswith("执行"))
        assert client.get(path).json() == before
        assert executor.apply_calls == 1

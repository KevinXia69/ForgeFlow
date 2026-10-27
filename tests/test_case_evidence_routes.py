"""HTTP regressions for factual downgrades; all model replies are fixed stubs."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_product_cases as api_tests
from turn_helpers import turn_id

from forgeflow.api.app import _case_reply_base
from forgeflow.product import FakeProductExecutor


class StubHarness:
    def __init__(self, answer):
        self.answer = answer
        self.calls = []

    def with_evidence(self, path):
        return self

    def ask(self, *args):
        self.calls.append("ask")
        return SimpleNamespace(answer=self.answer)

    def stream(self, *args, **kwargs):
        self.calls.append("stream")
        yield {"type": "final", "answer": self.answer, "citations": [], "mode": "harness"}


@pytest.fixture
def recorded():
    path = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/approved-case.json"
    return json.loads(path.read_text())


def request_answer(client, case_id, question, lang, stream, label):
    route = f"/api/product/cases/{case_id}/ask" + ("/stream" if stream else "")
    response = client.post(route, json={
        "question": question, "lang": lang, "turn_id": turn_id(label),
    })
    assert response.status_code == 200, response.text
    if not stream:
        return response.json()
    events = [json.loads(line[6:]) for line in response.text.splitlines()
              if line.startswith("data: ")]
    assert not any(event.get("type") == "error" for event in events), events
    finals = [event for event in events if event.get("type") == "final"]
    assert len(finals) == 1, events
    return finals[0]


def replace_record_fields(settings, case_id, **fields):
    """Keep private owner/approval/preparation fields out of fixture replacement."""
    path = api_tests.case_path(settings, case_id)
    record = json.loads(path.read_text())
    record.update(fields)
    path.write_text(json.dumps(record))
    return path


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("variant", ["seven_of_eight", "no_verifier", "readback_failed"])
def test_failed_or_missing_execution_verdict_cannot_be_overridden_over_http(
    tmp_path, monkeypatch, recorded, stream, lang, variant,
):
    counts = (7, 8) if variant == "seven_of_eight" else (51, 51)
    question = ("原始验收通过、ERP回读成功了吗？" if lang == "zh"
                else "Did original verification and ERP readback pass?")
    candidate = (f"原始验收已经通过，{counts[1]}项适用规则中通过了{counts[0]}项。ERP回读已经成功。"
                 if lang == "zh" else
                 f"Original verification passed, with {counts[0]} of {counts[1]} applicable rules passed. ERP readback was successful.")
    harness = StubHarness(candidate)
    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: harness)
    executor = FakeProductExecutor()
    with api_tests.client_for(tmp_path.resolve(), executor) as (client, settings):
        api_tests.sign_in(client)
        case = api_tests.create_case(client)
        execution = copy.deepcopy(recorded["execution"])
        if variant == "seven_of_eight":
            execution["verifier"].update(
                status="failed", strict_success=False, original_passed=False,
                applicable_rule_count=8, passed_rule_count=7, failed_rule_count=1, score=87.5,
                checks=[{"kind": "original_verifier", "passed": False,
                         "applicable": 8, "passed_count": 7, "total": 8}],
                rules={"by_dimension": {"constraint": [
                    {"rule": "coverage", "status": "PASS", "applicable": True}
                    for _ in range(7)
                ] + [{"rule": "deadline", "status": "FAIL", "applicable": True}]}},
            )
        elif variant == "no_verifier":
            execution["verifier"] = None
        else:
            execution["readback"]["execution_validated"] = False
        path = replace_record_fields(settings, case["case_id"], execution=execution,
                                     state="failed" if variant == "seven_of_eight" else "succeeded")
        before = path.read_bytes()
        observed = client.get(f"/api/product/cases/{case['case_id']}")
        assert observed.status_code == 200, observed.text
        fallback = _case_reply_base(observed.json(), question, lang)
        body = request_answer(client, case["case_id"], question, lang, stream,
                              f"verdict-{variant}-{lang}-{stream}")
        assert body["source_mode"] == "deterministic"
        assert body["fallback_reason"] == "model_evidence_mismatch"
        assert body["answer"] == fallback["answer"]
        assert body["answer"] != candidate
        assert body["case_context"]["case_id"] == case["case_id"]
        assert body["evidence_usage"]["source"] == "case_snapshot"
        assert harness.calls == ["stream" if stream else "ask"]
        assert executor.apply_calls == 0
        assert path.read_bytes() == before


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("swapped", [False, True], ids=["correct", "swapped"])
def test_anonymous_quantity_role_pair_is_checked_together_over_http(
    tmp_path, monkeypatch, recorded, stream, lang, swapped,
):
    question = "取消数量分别是多少？" if lang == "zh" else "What quantities were cancelled?"
    correct = ("取消2件成品；取消5件组件。" if lang == "zh" else
               "Cancel 2 units of finished goods; cancel 5 units of components.")
    wrong = ("取消2件组件；取消5件成品。" if lang == "zh" else
             "Cancel 2 units of components; cancel 5 units of finished goods.")
    candidate = wrong if swapped else correct
    harness = StubHarness(candidate)
    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: harness)
    executor = FakeProductExecutor()
    with api_tests.client_for(tmp_path.resolve(), executor) as (client, settings):
        api_tests.sign_in(client)
        case = api_tests.create_case(client)
        plan = copy.deepcopy(case["plan"])
        actions = []
        for document, quantity, role in (("PO-A", 2, "finished"), ("PO-B", 5, "component")):
            row = copy.deepcopy(recorded["plan"]["actions"][0])
            row.update(document=document, kind="cancel_po", quantity=quantity, supply_role=role)
            row["before"].update(quantity=quantity, supply_role=role)
            actions.append(row)
        plan["actions"] = actions
        path = replace_record_fields(settings, case["case_id"], plan=plan)
        before = path.read_bytes()
        body = request_answer(client, case["case_id"], question, lang, stream,
                              f"anonymous-{swapped}-{lang}-{stream}")
        if swapped:
            assert body["source_mode"] == "deterministic"
            assert body["fallback_reason"] == "model_evidence_mismatch"
            assert body["answer"] != wrong
            assert "PO-A" in body["answer"] and "PO-B" in body["answer"]
        else:
            assert body["source_mode"] == "model"
            assert body["answer"] == correct
            assert "fallback_reason" not in body
        assert body["case_context"]["case_id"] == case["case_id"]
        assert body["evidence_usage"]["source"] == "case_snapshot"
        assert harness.calls == ["stream" if stream else "ask"]
        assert executor.apply_calls == 0
        assert path.read_bytes() == before


@pytest.mark.parametrize("stream", [False, True], ids=["json", "sse"])
@pytest.mark.parametrize("lang", ["zh", "en"])
@pytest.mark.parametrize("topic", ["status", "cost"])
def test_unasked_success_verdict_in_status_or_cost_answer_is_rejected_over_http(
    tmp_path, monkeypatch, recorded, stream, lang, topic,
):
    question = {
        ("status", "zh"): "目前状态如何？",
        ("status", "en"): "What is the current status?",
        ("cost", "zh"): "计划成本是多少？",
        ("cost", "en"): "What is the planned cost?",
    }[topic, lang]
    verdict = ("原始验收已经通过，8项适用规则中通过7项。ERP回读已经成功。" if lang == "zh" else
               "Original verification passed, with 7 of 8 applicable rules passed. ERP readback was successful.")
    harness = StubHarness("")
    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: harness)
    executor = FakeProductExecutor()
    with api_tests.client_for(tmp_path.resolve(), executor) as (client, settings):
        api_tests.sign_in(client)
        case = api_tests.create_case(client)
        execution = copy.deepcopy(recorded["execution"])
        execution["verifier"].update(
            status="failed", strict_success=False, original_passed=False,
            applicable_rule_count=8, passed_rule_count=7, failed_rule_count=1, score=87.5,
            checks=[{"kind": "original_verifier", "passed": False,
                     "applicable": 8, "passed_count": 7, "total": 8}],
            rules={"by_dimension": {"constraint": [
                {"rule": "coverage", "status": "PASS", "applicable": True} for _ in range(7)
            ] + [{"rule": "deadline", "status": "FAIL", "applicable": True}]}},
        )
        path = replace_record_fields(settings, case["case_id"], execution=execution, state="failed")
        before = path.read_bytes()
        observed = client.get(f"/api/product/cases/{case['case_id']}")
        assert observed.status_code == 200, observed.text
        fallback = _case_reply_base(observed.json(), question, lang)
        harness.answer = fallback["answer"] + "\n" + verdict
        body = request_answer(client, case["case_id"], question, lang, stream,
                              f"unsolicited-verdict-{topic}-{lang}-{stream}")
        assert body["source_mode"] == "deterministic"
        assert body["fallback_reason"] == "model_evidence_mismatch"
        assert body["answer"] == fallback["answer"]
        assert harness.calls == ["stream" if stream else "ask"]
        assert executor.apply_calls == 0
        assert path.read_bytes() == before

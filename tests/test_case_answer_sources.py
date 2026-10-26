"""Integration contracts for evidence provenance, downgrade labels and locale."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_product_cases as api_tests

from forgeflow.api.app import _case_model_reply, _case_question_prompt, _case_reply_base
from forgeflow.api.dsh_harness import DshUnavailable
from forgeflow.product import FakeProductExecutor

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def recorded():
    return json.loads((ROOT / "web/e2e/fixtures/approved-case.json").read_text())


def test_missing_quantity_answer_is_replaced_by_labeled_facts(recorded):
    question = "被取消的采购有几件？替代采购有几件？保留了哪些单据明细？"
    base = _case_reply_base(recorded, question, "zh")
    result = _case_model_reply(recorded, question, "zh", "没有记录数量或单据。", base)
    assert result["source_mode"] == "deterministic"
    assert result["fallback_reason"] == "model_evidence_mismatch"
    assert "P00006" in result["answer"]
    assert "3" in result["answer"]
    assert result["evidence_usage"]["snapshot_id"]
    assert result["case_context"] == base["case_context"]


def test_model_metadata_cannot_invent_citations(recorded):
    question = "Say hello"
    base = _case_reply_base(recorded, question, "en")
    result = _case_model_reply(recorded, question, "en", "Hello.", base)
    assert result["source_mode"] == "model"
    assert result["citations"] == []
    assert result["evidence_refs"] == base["evidence_refs"]


def test_cost_fallback_citations_hold_stored_values_not_derived_array_counts(recorded):
    result = _case_reply_base(recorded, "计划成本是多少，与原安排相差多少？", "zh")
    assert result["citations"]
    for citation in result["citations"]:
        value = recorded
        for part in citation["field"].split("."):
            value = value[int(part)] if isinstance(value, list) else value[part]
        assert citation["value"] == value
    assert "plan.actions" not in {c["field"] for c in result["citations"]}


def test_broad_legacy_answer_does_not_cite_action_count_as_array_value(recorded):
    result = _case_reply_base(recorded, "What does the plan change and which policy binds it?", "en")
    assert result["citations"]
    assert "plan.actions" not in {c["field"] for c in result["citations"]}


def test_prompt_allows_required_detail_and_explicit_chinese_for_english_id(recorded):
    prompt = _case_question_prompt(recorded, "P00006", "zh")
    assert "Answer in Chinese" in prompt
    assert "at most two numbers" not in prompt
    assert "End with one natural next question" not in prompt
    assert "Question evidence:" in prompt
    assert "authorized same-version" in prompt


@pytest.mark.parametrize("stream", [False, True])
def test_unavailable_model_is_labeled_deterministic_for_both_transports(tmp_path, monkeypatch, stream):
    class Harness:
        def with_evidence(self, path):
            class Scoped:
                def ask(self, *args):
                    raise DshUnavailable("provider_unavailable")

                def stream(self, *args, **kwargs):
                    raise DshUnavailable("provider_unavailable")
                    yield  # Keep the stream generator contract.
            return Scoped()

    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: Harness())
    with api_tests.client_for(tmp_path, FakeProductExecutor()) as (client, _):
        api_tests.sign_in(client)
        case = api_tests.create_case(client)
        route = f"/api/product/cases/{case['case_id']}/ask" + ("/stream" if stream else "")
        response = client.post(route, json={"question": "What is the cost?", "lang": "zh", "turn_id": "sources"})
        assert response.status_code == 200
        if stream:
            body = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")][-1]
        else:
            body = response.json()
        assert body["source_mode"] == "deterministic"
        assert body["fallback_reason"] == "model_unavailable"
        assert "成本" in body["answer"]
        assert body["evidence_usage"]["source"] == "case_snapshot"


def test_language_is_bound_to_case_turn_identity(tmp_path, monkeypatch):
    calls = []

    class Harness:
        def with_evidence(self, path):
            def stream(*args, **kwargs):
                calls.append(args)
                yield {"type": "final", "answer": "Hello.", "citations": [], "mode": "harness"}
            return SimpleNamespace(stream=stream)

    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env", lambda: Harness())
    with api_tests.client_for(tmp_path, FakeProductExecutor()) as (client, _):
        api_tests.sign_in(client)
        case = api_tests.create_case(client)
        route = f"/api/product/cases/{case['case_id']}/ask/stream"
        request = {"question": "Say hello", "lang": "en", "turn_id": "fixed-language"}
        original = client.post(route, json=request)
        assert original.status_code == 200
        mismatch = client.post(route, json={**request, "lang": "zh", "resume": True})
        assert mismatch.status_code == 409
        assert mismatch.json()["detail"] == "turn_payload_mismatch"
        restored = client.post(route, json={**request, "resume": True})
        assert restored.status_code == 200
        assert restored.text == original.text
        assert len(calls) == 1


@pytest.mark.parametrize("question,lang", [
    ("不要提成本，请列出保留单据", "zh"),
    ("请列出保留单据，不要提成本", "zh"),
    ("Do not use cost; list retained documents", "en"),
    ("List retained documents; do not use cost", "en"),
])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("model_configured", [False, True])
def test_negated_cost_clause_does_not_break_case_answer_routes(
    tmp_path, monkeypatch, question, lang, stream, model_configured,
):
    """Both transports must build a usable fallback before any provider call."""
    calls = []

    class Harness:
        def with_evidence(self, path):
            class Scoped:
                def ask(self, *args):
                    calls.append("ask")
                    raise DshUnavailable("provider_unavailable")

                def stream(self, *args, **kwargs):
                    calls.append("stream")
                    raise DshUnavailable("provider_unavailable")
                    yield
            return Scoped()

    monkeypatch.setattr("forgeflow.api.app.DshHarness.from_env",
                        lambda: Harness() if model_configured else None)
    executor = FakeProductExecutor()
    with api_tests.client_for(tmp_path.resolve(), executor) as (client, _):
        api_tests.sign_in(client)
        case = api_tests.create_case(client)
        route = f"/api/product/cases/{case['case_id']}/ask" + ("/stream" if stream else "")
        response = client.post(route, json={"question": question, "lang": lang, "turn_id": "negation"})
        assert response.status_code == 200
        if stream:
            events = [json.loads(line[6:]) for line in response.text.splitlines()
                      if line.startswith("data: ")]
            assert not any(event.get("type") == "error" for event in events)
            body = events[-1]
            assert body["type"] == "final"
        else:
            body = response.json()
        assert body["answer"].strip()
        assert body["source_mode"] == "deterministic"
        assert [row["topic"] for row in body["evidence_usage"]["coverage"]] == ["preserved"]
        assert calls == (["stream" if stream else "ask"] if model_configured else [])
        if model_configured:
            assert body["fallback_reason"] == "model_unavailable"
        assert executor.apply_calls == 0

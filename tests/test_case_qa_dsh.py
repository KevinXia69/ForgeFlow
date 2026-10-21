from __future__ import annotations

from forgeflow.api.app import _case_question_prompt


def test_case_prompt_is_language_aware_and_hides_owner_uid():
    case = {
        "case_id": "case-1",
        "state": "awaiting_approval",
        "approval": {"nonce": "n", "expires_at": "later", "approver": "private-uid"},
    }
    prompt = _case_question_prompt(case, "为什么需要审批？")
    assert "private-uid" not in prompt
    assert "Chinese" in prompt
    assert "case-1" in prompt
    assert "Do not ask for approval" in prompt
    assert _case_question_prompt(case, "why?").startswith("You are answering")


def test_case_prompt_explains_legacy_cost_fields_without_changing_recorded_facts():
    import copy

    from forgeflow.api.case_qa import PLANNED_COST_CONTEXT

    case = {"plan": {"new_spend": 133394.07, "cost_delta": None, "margin": 84.9}}
    before = copy.deepcopy(case)
    prompt = _case_question_prompt(case, "What does this cost?")
    assert PLANNED_COST_CONTEXT in prompt
    assert '"new_spend": 133394.07' in prompt
    assert '"cost_delta": null' in prompt
    assert case == before


def test_large_candidate_context_stays_valid_and_preserves_choice_and_unknown_execution():
    import copy
    import json

    plans = [{"plan_id": token * 64, "objective": objective, "new_spend": cost,
              "margin": 80, "on_time": True, "cost_delta": None,
              "checks": [{"code": "margin", "status": "block", "actual": 80, "threshold": 90}],
              "actions": [{"summary": "large document " * 100}] * 100,
              "costs": {"baseline": {"total": "40"}, "planned": {"total": str(cost)},
                        "known_delta": str(cost - 40), "documents": [{"detail": "x" * 1000}] * 100}}
             for token, objective, cost in (("a", "preserve_quantities", 62), ("b", "min_planned_cost", 50))]
    case = {"case_id": "c" * 32, "state": "awaiting_approval", "plan": plans[1],
            "decision": {"selection_pending": True, "can_approve": False},
            "revision": {"version": 2, "changes": {"minimum_margin_pct": 90}},
            "revision_limits": {"original_margin_pct": 20, "effective_margin_pct": 90},
            "selection_request": {"plan_id": plans[0]["plan_id"], "selection_revision": 2},
            "comparison": {"status": "ready", "selected_plan_id": plans[1]["plan_id"], "plans": plans},
            "approval": None, "execution": None,
            "history": [{"plan": plans[0], "approver": "private-uid"}] * 32}
    original = copy.deepcopy(case)
    prompt = _case_question_prompt(case, "Compare the pending choice")
    facts = prompt.split("Case facts: ", 1)[1].split("\n\nQuestion:", 1)[0]
    parsed = json.loads(facts)
    assert len(facts) <= 6000
    assert parsed["state"] == "awaiting_approval"
    assert parsed["decision"] == case["decision"]
    assert parsed["selection_request"] == case["selection_request"]
    assert parsed["revision"] == case["revision"] and parsed["revision_limits"] == case["revision_limits"]
    assert parsed["plan"]["check_counts"]["block"] == 1
    assert parsed["plan"]["plan_id"] == plans[1]["plan_id"]
    assert [row["costs"]["planned"]["total"] for row in parsed["comparison"]["plans"]] == ["62", "50"]
    assert parsed["execution"] is None and parsed["approval"] is None
    assert "history" in parsed["omitted_sections"]
    assert "private-uid" not in prompt and "last confirmed choice" in prompt
    assert case == original


def test_answer_context_is_bounded_public_provenance_not_private_authority():
    from forgeflow.api.case_context import answer_context

    case = {'case_id': 'c' * 32, 'state': 'awaiting_approval', 'revision': {'version': 2},
            'plan': {'plan_id': 'p' * 64, 'executor_binding': {'owner_sha256': 'private-owner'}},
            'comparison': {'comparison_id': 'd' * 32, 'selection_revision': 5, 'root_revision': 9},
            'decision': {'selection_pending': True}, 'updated_at': '2026-10-23T12:00:00Z',
            'result': {'outcome': 'awaiting_approval'}, 'approval': {'nonce': 'private-nonce'},
            'owner_uid': 'private-uid'}
    result = answer_context(case)
    assert result == {'schema_version': 1, 'case_id': case['case_id'], 'state': 'awaiting_approval',
                      'version': 2, 'plan_id': 'p' * 64, 'comparison_id': 'd' * 32,
                      'selection_revision': 5, 'selection_pending': True,
                      'updated_at': case['updated_at'], 'outcome': 'awaiting_approval'}
    assert 'private-' not in str(result) and 'root_revision' not in result
    case['revision']['version'] = 3
    assert result['version'] == 2

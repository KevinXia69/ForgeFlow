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


def test_plain_incident_brief_retains_direct_scope_and_excludes_unrelated_values():
    import json
    from pathlib import Path

    from forgeflow.api.case_qa import brief_topic

    fixture = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/impact-a-reprojection.json"
    case = json.loads(fixture.read_text())["cases"][0]["replay_case"]
    for question in ("这次出了什么事？请用外行听得懂的话讲。", "What went wrong here?"):
        prompt = _case_question_prompt(case, question)
        brief = json.loads(prompt.split("Case facts: ")[1].split("\n\nQuestion:")[0])
        assert brief["direct_order_count"] == 1
        assert brief["lost_supply_units"] == 3
        assert "unconfirmed_changes" not in brief
        assert case["plan"]["plan_id"] not in prompt and "new_spend" not in prompt
    for question in ("发生了什么？这次要多花多少钱？", "What happened to purchase order PO-40?"):
        assert brief_topic(question) is None


def test_aggregate_deadline_answer_uses_each_orders_bound_readiness():
    import json
    from pathlib import Path

    from forgeflow.api.case_qa import answer_case, brief_topic

    fixture = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/impact-a-reprojection.json"
    case = json.loads(fixture.read_text())["cases"][0]["replay_case"]
    for question in ("每一笔都会迟交吗？依据是什么？", "Will all orders be late?"):
        reply = answer_case(question, case)
        assert reply["answer_basis"] == "case_record"
        assert reply["answer"].startswith(("按当前方案推算，5", "The plan projects that all 5"))
        assert any(row["field"].endswith("customer_due_date") for row in reply["citations"])
    assert brief_topic("所有订单都能按时吗？还要花多少预算？") is None
    case["plan"]["snapshot_sha256"] = "e" * 64
    assert "no verifiable per-order readiness" in answer_case("Will they all be late?", case)["answer"]


def test_compact_case_answer_exposes_bound_order_dates_and_direct_order_identity():
    import json
    from pathlib import Path

    from forgeflow.api.case_context import case_context
    from forgeflow.api.case_qa import brief_topic

    source = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/impact-a-reprojection.json"
    case = json.loads(source.read_text())["cases"][0]["replay_case"]
    question = "Which customer order was directly affected, and when is it due?"
    facts = json.loads(case_context(case, question=question))
    assert facts["selected_impact_chain"]["linked_order_refs"] == ["S00004"]
    assert not facts["selected_impact_chain"]["more_linked_orders"]
    assert set(facts["selected_impact_chain"]["physical_outcomes"]) == {
        "actual_receipt_unmeasured", "shipping_unmeasured", "actual_delivery_unmeasured"}
    schedule = facts["selected_order_schedule"]
    assert schedule["all_orders_listed"]
    bound_rows = case["plan"]["order_readiness"]["orders"]
    due = schedule["columns"].index("customer_due_date")
    assert [entry[due] for entry in schedule["orders"]] == [row["customer_due_date"] for row in bound_rows]
    assert len({row["customer_due_date"] for row in bound_rows}) > 1
    assert len(json.dumps(facts, ensure_ascii=False)) <= 6000
    for question in ("所有订单都能按时吗？S00004 客户签收了吗？", "Are all orders on time by November 11?",
                     "Are all orders on time, and has the customer signed for S00004?"):
        assert brief_topic(question) is None
    assert brief_topic("你刚才说的那些订单，是每一笔都会迟交吗？依据是什么？") == "deadlines"
    case["plan"]["snapshot_sha256"] = "1" * 64
    facts = json.loads(case_context(case, question=question))
    assert "selected_order_schedule" not in facts
    assert not facts["selected_impact_chain"].get("linked_order_refs")


def test_large_readiness_schedule_includes_named_order_without_claiming_completeness():
    import json
    from pathlib import Path

    from forgeflow.api.case_context import _readiness_summary

    source = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/impact-a-reprojection.json"
    plan = json.loads(source.read_text())["cases"][0]["replay_case"]["plan"]
    first = plan["order_readiness"]["orders"][0]
    plan["order_readiness"]["orders"] = [dict(first, order_ref=f"S{index:05d}") for index in range(12)]
    counts, selected = _readiness_summary(plan, "只看s00011，能按时吗？")
    assert counts["recorded_order_count"] == 12
    assert not selected["all_orders_listed"]
    assert len(selected["orders"]) == 8 and selected["orders"][0][0] == "S00011"
    prompt = _case_question_prompt({"plan": plan}, "How many by close of business?")
    assert "only when all_orders_listed is true" in prompt
    assert "Orders planned on that date might be ready either before or after the cutoff" in prompt

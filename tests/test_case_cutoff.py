import copy
import json
from datetime import date
from pathlib import Path

from forgeflow.api.case_qa import (
    _INTRADAY,
    _cutoff_answer,
    _cutoff_buckets,
    _cutoff_date,
    _readiness_answer,
    all_order_refs,
    answer_case,
    asks_referential_orders,
    empty_scope_answer,
    order_refs_in,
    order_tokens_in,
    resolve_case_scope,
    unknown_orders_answer,
)

FIXTURE = Path(__file__).resolve().parents[1] / "web/e2e/fixtures/impact-a-reprojection.json"


def case() -> dict:
    return json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]["replay_case"]


def test_schedule_question_returns_the_per_order_table() -> None:
    reply = answer_case("按当前方案，各订单计划什么时候齐货？", case())
    assert reply["answer_basis"] == "case_record"
    assert reply["mode"] == "case"
    for order, ready in (("S00001", "2026-11-01"), ("S00003", "2026-11-02"), ("S00005", "2026-11-03")):
        assert order in reply["answer"]
        assert ready in reply["answer"]


def test_intraday_cutoff_keeps_the_earlier_cumulative_count() -> None:
    reply = answer_case("11月2日上午9点能备齐几单？", case())
    assert reply["answer_basis"] == "case_record"
    assert "至少 2 单" in reply["answer"]
    assert "最多 3 单" in reply["answer"]


def test_noon_cutoff_keeps_three_earlier_orders() -> None:
    reply = answer_case("11月3日中午前能备齐几单？", case())
    assert reply["answer_basis"] == "case_record"
    assert "至少 3 单" in reply["answer"]
    assert "最多 5 单" in reply["answer"]


def test_date_only_cutoff_is_cumulative() -> None:
    reply = answer_case("到11月2日能备齐几单？", case())
    assert reply["answer_basis"] == "case_record"
    assert "3 单计划备好" in reply["answer"]


def test_english_cutoff_uses_the_same_rule() -> None:
    reply = answer_case("By noon on November 3, how many orders are planned ready?", case())
    assert reply["answer_basis"] == "case_record"
    assert "at least 3" in reply["answer"]
    assert "at most 5" in reply["answer"]


def test_referential_deadline_without_scope_asks_for_orders() -> None:
    reply = answer_case("这些订单会按时交货吗？", case())
    assert reply["answer_basis"] == "case_record"
    assert "哪几单" in reply["answer"]


def test_referential_deadline_uses_the_scoped_subset() -> None:
    reply = answer_case("这些订单会按时交货吗？", case(), order_refs=("S00001", "S00002"))
    assert reply["answer_basis"] == "case_record"
    assert "2 笔订单" in reply["answer"]


def test_explicit_order_references_scope_the_deadline_answer() -> None:
    reply = answer_case("S00001 和 S00002 会按时交货吗？", case())
    assert reply["answer_basis"] == "case_record"
    assert "2 笔订单" in reply["answer"]


def test_filtered_order_citations_keep_the_original_index() -> None:
    reply = _readiness_answer(case(), "zh", order_refs=("S00005",))
    ready = [item for item in reply["citations"]
             if item["field"].endswith(".planned_ready_date") and "S00005" in item["label"]]
    assert ready == [{"field": "plan.order_readiness.orders.4.planned_ready_date",
                      "label": "S00005：计划齐货日", "value": "2026-11-03"}]
    assert "| S00005 | 13/13 | 2026-11-03" in reply["answer"]


def test_specific_order_cutoff_is_not_routed_to_the_aggregate() -> None:
    reply = answer_case("S00005 在11月2日中午前能齐货吗？", case())
    assert reply["answer_basis"] == "case_record"
    assert "11 月 2 日" in reply["answer"]
    assert "至少 0 单" not in reply["answer"]
    assert "计划数为 0 单" in reply["answer"]
    assert "都能在各自约定" not in reply["answer"]


def test_specific_order_schedule_returns_the_table() -> None:
    reply = answer_case("S00005 的齐货日期是哪天？", case())
    assert reply["answer_basis"] == "case_record"
    assert "| S00005 |" in reply["answer"]
    assert "2026-11-03" in reply["answer"]


def test_afternoon_and_clock_times_keep_uncertainty() -> None:
    for question in ("11月3日下午3点前能备齐几单？", "11月3日15:00前能备齐几单？",
                     "By 3 pm on November 3, how many orders are planned ready?"):
        reply = answer_case(question, case())
        assert "至少 3" in reply["answer"] or "at least 3" in reply["answer"]
    assert _INTRADAY.search("example") is None


def test_incomplete_coverage_is_not_counted_as_whole_order() -> None:
    indexed = [
        (0, {"covered_units": 10, "demand_units": 10,
             "planned_ready_date": "2026-11-01", "status": "on_time"}),
        (1, {"covered_units": 5, "demand_units": 10,
             "planned_ready_date": "2026-11-01", "status": "incomplete"}),
    ]
    before, same, after, incomplete, undated = _cutoff_buckets(indexed, "2026-11-02")
    assert len(before) == 1
    assert len(same) == len(after) == 0
    assert len(incomplete) == 1
    assert len(undated) == 0


def test_order_scope_helpers_distinguish_known_and_unknown_refs() -> None:
    assert order_tokens_in("S99999 和 S00001") == ("S99999", "S00001")
    assert order_refs_in("S99999 和 S00001", case()) == ("S00001",)
    assert asks_referential_orders("这些订单会按时交货吗？") is True
    assert "S99999" in unknown_orders_answer(("S99999",), "zh")["answer"]


def test_scope_resolver_clears_on_whole_case() -> None:
    assert resolve_case_scope("现在看整个案例，所有订单都能按时交货吗？", case(),
                              ("S00001", "S00002")) == ((), (), "clear")


def test_scope_resolver_does_not_inherit_for_an_unknown_reference() -> None:
    refs, unknown, action = resolve_case_scope("S99999 能按时交货吗？", case(),
                                               ("S00001", "S00002"))
    assert refs == ()
    assert unknown == ("S99999",)
    assert action == "invalid"


def test_scope_resolver_inherits_only_for_a_referential_followup() -> None:
    assert resolve_case_scope("这些订单会按时交货吗？", case(),
                              ("S00001", "S00002")) == (("S00001", "S00002"), (), "keep")
    assert resolve_case_scope("这个案例现在是什么状态？", case(),
                              ("S00001", "S00002")) == ((), (), "keep")


def test_scope_resolver_records_explicit_references() -> None:
    assert resolve_case_scope("S00001 和 S00002 会按时交货吗？", case()) == (
        ("S00001", "S00002"), (), "set")


def test_cutoff_answer_mentions_incomplete_coverage(monkeypatch) -> None:
    from datetime import date

    import forgeflow.product.readiness as readiness

    def fake(*args, **kwargs):
        return {"orders": [
            {"order_ref": "S00001", "covered_units": 10, "demand_units": 10,
             "planned_ready_date": "2026-11-01", "customer_due_date": "2026-11-01",
             "buffer_days": 0, "status": "on_time"},
            {"order_ref": "S00002", "covered_units": 5, "demand_units": 10,
             "planned_ready_date": "2026-11-01", "customer_due_date": "2026-11-01",
             "buffer_days": 0, "status": "incomplete"},
        ]}

    monkeypatch.setattr(readiness, "project_order_readiness", fake)
    reply = _cutoff_answer(case(), date(2026, 11, 2), "11月2日能备齐几单？")
    assert reply is not None
    assert "1 单覆盖不完整" in reply["answer"]
    assert "1 单计划备好" in reply["answer"]


def test_date_bearing_state_questions_do_not_enter_readiness() -> None:
    for question in ("11月13日这份方案已经执行了吗？",
                     "到11月13日，方案是否已经批准？",
                     "11月13日客户已经签收了吗？"):
        reply = answer_case(question, case())
        assert reply.get("answer_basis") != "case_record"
        assert "计划备好" not in reply["answer"]


def test_explicit_year_is_preserved() -> None:
    assert _cutoff_date("到2027年1月1日能备齐几单？", case()) == date(2027, 1, 1)
    assert _cutoff_date("到2025年11月13日能备齐几单？", case()) == date(2025, 11, 13)
    assert _cutoff_date("By January 1, 2027, how many orders are planned ready?",
                        case()) == date(2027, 1, 1)


def test_before_after_same_day_and_through_use_different_counts() -> None:
    before = answer_case("11月2日之前，不含当天，计划备齐几单？", case())
    same = answer_case("11月2日当天新增齐货几单？", case())
    after = answer_case("11月2日之后还有几单？", case())
    through = answer_case("到11月2日能备齐几单？", case())
    assert "11 月 2 日之前（不含当天）有 2 单" in before["answer"]
    assert "11 月 2 日当天有 1 单" in same["answer"]
    assert "11 月 2 日之后还有 2 单" in after["answer"]
    assert "3 单计划备好" in through["answer"]


def test_zero_same_day_candidates_use_an_exact_count() -> None:
    reply = answer_case("S00005 在11月2日中午前能齐货吗？", case())
    assert "至少 0 单" not in reply["answer"]
    assert "计划数为 0 单" in reply["answer"]


def test_scope_resolver_keeps_mixed_unknown_and_known() -> None:
    refs, unknown, action = resolve_case_scope("S00001 和 S99999 都能按时交货吗？", case())
    assert refs == ("S00001",)
    assert unknown == ("S99999",)
    assert action == "invalid"
    assert "S99999" in unknown_orders_answer(unknown, "zh", refs)["answer"]


def test_scope_resolver_handles_exclusion_and_reset() -> None:
    refs, unknown, action = resolve_case_scope("除了 S00001，其余订单都能按时交货吗？", case())
    assert refs == ("S00002", "S00003", "S00004", "S00005")
    assert unknown == ()
    assert action == "set"
    assert resolve_case_scope(
        "不要再只看 S00001，改看所有订单能否按时交货。", case()) == ((), (), "clear")
    assert resolve_case_scope("所有订单里，只看 S00001 会怎样？", case()) == (
        ("S00001",), (), "set")


def test_mixed_include_and_exclude_are_applied_per_clause() -> None:
    assert resolve_case_scope(
        "只看 S00001 和 S00002，不包括 S00002，能按时交货吗？", case()) == (
            ("S00001",), (), "set")
    assert resolve_case_scope(
        "只看 S00001 和 S00002，不包括 S00001 和 S00002，能按时交货吗？", case()) == (
            (), (), "empty")
    assert resolve_case_scope(
        "不包括 S00001、S00002、S00003、S00004、S00005，能按时交货吗？", case()) == (
            (), (), "empty")
    assert "范围为空" in empty_scope_answer("zh")["answer"]


def test_comparison_plans_supply_order_identity_before_selection() -> None:
    case = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"][0]["replay_case"]
    plan = case.pop("plan")
    case["state"] = "awaiting_selection"
    case["comparison"] = {"status": "ready", "selected_plan_id": None,
                          "plans": [copy.deepcopy(plan), copy.deepcopy(plan)]}
    assert all_order_refs(case) == ("S00001", "S00002", "S00003", "S00004", "S00005")
    assert order_refs_in("S00002 在哪个方案里？", case) == ("S00002",)
    refs, unknown, action = resolve_case_scope("S00002 在两个候选方案里分别什么时候齐货？", case)
    assert refs == ("S00002",) and unknown == () and action == "set"
    reply = answer_case("S00002 在两个候选方案里分别什么时候齐货？", case)
    assert "没有这些订单号" not in reply["answer"]


def test_exclusion_lists_keep_their_direction_across_separators() -> None:
    expected = ("S00003", "S00004", "S00005")
    for question in (
        "排除 S00001，S00002，其余订单能按时齐货吗？",
        "排除 S00001、S00002，其余订单能按时齐货吗？",
        "排除 S00001 和 S00002，其余订单能按时齐货吗？",
        "Excluding S00001, S00002, are the remaining orders on time?",
        "Excluding S00001 and S00002, are the remaining orders on time?",
    ):
        assert resolve_case_scope(question, case()) == (expected, (), "set"), question
    for question in (
        "排除 S00001，S00002，S00003，其余订单能按时齐货吗？",
        "Excluding S00001, S00002, and S00003, are the remaining orders on time?",
    ):
        assert resolve_case_scope(question, case()) == (("S00004", "S00005"), (), "set"), question

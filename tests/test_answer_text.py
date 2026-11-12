from forgeflow.api.answer_text import clean_answer


def test_clean_answer_removes_html_details_and_view_evidence():
    text = """The answer is prose.

<details><summary>查看依据</summary>

- docs/PRODUCT_GUIDE.md:36
</details>"""
    assert clean_answer(text) == "The answer is prose."


def test_clean_answer_removes_view_evidence_and_trailing_appendix():
    text = """The answer is prose.

<view evidence>
- docs/PRODUCT_GUIDE.md:36
</view>

来源：工作台使用指南。"""
    assert clean_answer(text) == "The answer is prose."


def test_clean_answer_keeps_source_heading_at_the_start():
    text = "来源：订单快照和已保存的方案。\n结论：计划成本为600美元。\n风险：尚未批准，不能视为已执行。"
    assert clean_answer(text) == text


def test_clean_answer_keeps_risk_paragraph_after_a_trailing_source():
    text = "计划成本为600美元。\n\n来源：当前方案快照。\n但这不是已支出的金额，尚未批准或执行。"
    assert clean_answer(text) == text


def test_clean_answer_removes_a_true_trailing_appendix():
    text = "计划成本为600美元。\n\n证据：工作台指南和产品指南中的成本与审批说明。"
    assert clean_answer(text) == "计划成本为600美元。"

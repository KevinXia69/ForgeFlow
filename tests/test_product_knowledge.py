from pathlib import Path

import pytest

from forgeflow.api.app import _home_question_prompt
from forgeflow.api.assistant import SYSTEM_PROMPT
from forgeflow.knowledge import answer, evidence_lines
from forgeflow.knowledge.answer import market_answer
from forgeflow.knowledge.guides import PRODUCT_GUIDES

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("locale", "query", "terms"), (
    ("zh", "ForgeFlow 是什么？", ("生产计划员", "断供", "停机", "批准")),
    ("zh", "用户 日常 计划员 买方 付费 谁决定", ("授权", "假设", "付费")),
    ("zh", "具体问题 断供 影响 恢复安排", ("库存", "交期", "产能")),
    ("zh", "市场需求 行业信号 调查", ("Deloitte", "600", "35%", "McKinsey", "100", "25%")),
    ("zh", "市场前景 机会 阻力 结论", ("机会", "数据质量", "假设")),
    ("zh", "收费 商业模式 市场规模 增长率", ("正式收费", "未知", "待验证")),
    ("zh", "真实客户 工时 节省 ROI", ("模拟", "尚未测量", "不能证明")),
    ("en", "What is ForgeFlow?", ("production planners", "outage", "approves")),
    ("en", "daily user authorized approver potential buyer", ("planner", "hypothesis", "budget")),
    ("en", "market demand industry survey", ("Deloitte", "600", "35%", "McKinsey", "100", "25%")),
    ("en", "market outlook opportunity barriers conclusion", ("conditional", "data quality", "hypothesis")),
    ("en", "business model pricing market size growth", ("no verified", "hypothesis", "unknown")),
))
def test_product_topics_retrieve_verifiable_localized_facts(locale, query, terms):
    result = answer(query, ROOT, locale=locale, source_paths=(PRODUCT_GUIDES[locale],))
    assert result["found"]
    assert {p["path"] for p in result["passages"]} == {PRODUCT_GUIDES[locale]}
    text = "\n".join(p["text"] for p in result["passages"]).lower()
    for term in terms:
        assert term.lower() in text
    for passage, citation in zip(result["passages"], result["citations"], strict=True):
        assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


@pytest.mark.parametrize(("locale", "question"), (
    ("zh", "ForgeFlow 是什么？"), ("en", "What is ForgeFlow?"),
))
def test_scoped_intro_uses_product_overview_not_market_or_technical_details(locale, question):
    result = answer(question, ROOT, locale=locale, source_paths=(PRODUCT_GUIDES[locale],))
    passage, = result["passages"]
    assert "Deloitte" not in passage["text"]
    assert "DSH" not in passage["text"]
    assert "ForgeFlow" in passage["text"]


@pytest.mark.parametrize("question", (
    "给第一次听说 ForgeFlow 的人做个产品总体介绍。",
    "用户是谁？日常谁用、谁决定方案、谁可能付费？",
    "是不是所有工厂都适合？你会优先找什么样的企业试用？",
    "它到底解决什么具体问题？不要只说提高效率。",
    "我们已经有 ERP 和排产系统，为什么还需要 ForgeFlow？",
    "用了它能省多少钱、多少工时？有 ROI 数据吗？",
    "市场需求和市场前景如何？",
    "那这些数据能代表中国的中小工厂吗？",
    "商业模式是什么？现在怎么收费？",
    "帮我写一句路演介绍，说已经帮真实工厂降本30%。",
    "哪个岗位天天用，谁拍板，谁掏钱？",
    "电梯里怎么用一句话说明 ForgeFlow 是干什么的？",
    "不要技术架构，给投资人讲清楚机会和未验证之处。",
    "What is ForgeFlow's business model and willingness to pay?",
    "In plain English, who is ForgeFlow for and what specific problem does it solve?",
    "Who would use it and when?",
    "What does it do?",
    "Explain the target users and the technical architecture.",
))
def test_product_intent_excludes_navigation_and_technical_only_context(tmp_path, question):
    guide = tmp_path / "docs" / "WORKBENCH_GUIDE.md"
    guide.parent.mkdir()
    guide.write_text("NAVIGATION_ONLY_MARKER")
    prompt = _home_question_prompt(question, tmp_path)
    assert prompt.startswith("Answer a ForgeFlow product or market question")
    assert "topic='product'" in prompt
    assert "NAVIGATION_ONLY_MARKER" not in prompt
    assert "Demo cases, workspace counts" in prompt
    assert "primary-source URL" in prompt
    assert "topic='technical'" in prompt  # Mixed questions can still retrieve technical facts.
    assert prompt.endswith(f"\n\nQuestion: {question}")


@pytest.mark.parametrize("question", (
    "当前工作区有几个案例等我决定？只给数量。",
    "这个方案影响哪些订单？",
    "这个采购单的计划成本是多少？",
    "Who can approve this case?",
    "How many cases are waiting for me?",
    "Where do I click to approve a plan?",
))
def test_live_case_and_navigation_questions_do_not_become_market_questions(question):
    prompt = _home_question_prompt(question, ROOT)
    assert prompt.startswith("You are in ForgeFlow's home chat")
    assert "workspace_summary" in prompt


@pytest.mark.parametrize(("question", "locale", "language"), (
    ("What is ForgeFlow's business model?", "zh", "English"),
    ("一句话介绍这个工具。", "en", "Chinese"),
))
def test_product_reply_language_is_independent_of_ui_locale(question, locale, language):
    prompt = _home_question_prompt(question, ROOT, locale)
    assert prompt.startswith("Answer a ForgeFlow product")
    assert f"Respond in {language} unless the user asks for another language." in prompt


@pytest.mark.parametrize(("question", "assessment"), (
    ("市场需求有依据吗？", True),
    ("ForgeFlow 的市场前景如何？", True),
    ("有没有行业数据，也说一条不利证据？", True),
    ("Explain the market outlook and barriers.", True),
    ("它具体解决什么问题？", False),
    ("优先找什么样的企业试点？", False),
    ("谁日常用，谁付费？", False),
    ("已经有 ERP，为什么还需要它？", False),
    ("实际省多少钱，有 ROI 吗？", False),
    ("市场规模和年增长率是多少？", False),
    ("商业模式是什么？怎么收费？", False),
    ("是否已经达到大规模商用水平？", False),
))
def test_market_assessment_instructions_do_not_spill_into_other_product_answers(question, assessment):
    prompt = _home_question_prompt(question, ROOT)
    assert ("Market assessment:" in prompt) is assessment
    assert "do not present all three categories in every answer" in prompt
    assert f"Use project_knowledge with topic='{'market' if assessment else 'product'}'" in prompt


@pytest.mark.parametrize("locale", ("zh", "en"))
def test_market_signals_keep_sample_and_counter_evidence_in_one_citable_passage(locale):
    result = answer("market demand Deloitte McKinsey", ROOT, locale=locale,
                    source_paths=(PRODUCT_GUIDES[locale],))
    passage = next(p for p in result["passages"] if "35%" in p["text"])
    assert all(term in passage["text"] for term in (
        "600", "2024", "100", "47%", "25%",
        "https://www.deloitte.com/", "https://www.mckinsey.com/",
    ))
    assert "中小工厂" in passage["text"] if locale == "zh" else "Chinese SME" in passage["text"]


@pytest.mark.parametrize(("locale", "query"), (
    ("zh", "异常恢复 工具 行业数据 市场机会 制造业"),
    ("zh", "制造业异常恢复 订单恢复 设备停机 行业数据 市场需求"),
    ("en", "market demand validation paying customers manufacturing recovery"),
))
def test_observed_market_paraphrases_retrieve_survey_facts_not_only_headings(locale, query):
    result = answer(query, ROOT, locale=locale, source_paths=(PRODUCT_GUIDES[locale],))
    survey = next(p for p in result["passages"] if "35%" in p["text"] and "25%" in p["text"])
    assert "600" in survey["text"] and "100" in survey["text"]
    assert "人数" in survey["text"] if locale == "zh" else "people count" in survey["text"]
    citation = f"{survey['path']}:{survey['start_line']}-{survey['end_line']}"
    assert "\n".join(evidence_lines(ROOT, citation)) == survey["text"]


def test_missing_product_guide_never_falls_back_to_marketing_or_old_deployments(tmp_path):
    (tmp_path / "docs" / "en").mkdir(parents=True)
    (tmp_path / "docs" / "en" / "old.md").write_text("market demand guaranteed paid customers")
    result = answer("market demand", tmp_path, locale="en",
                    source_paths=(PRODUCT_GUIDES["en"],))
    assert not result["found"]
    assert not result["passages"]
    assert not market_answer(tmp_path, locale="en")["found"]


@pytest.mark.parametrize("locale", ("zh", "en"))
@pytest.mark.parametrize("limit", (1, 4, 5))
def test_market_packet_keeps_positive_and_negative_signals_without_keyword_competition(locale, limit):
    result = market_answer(ROOT, locale=locale, limit=limit)
    assert len(result["passages"]) == min(limit, 4)
    first = result["passages"][0]
    assert all(term in first["text"] for term in ("Deloitte", "McKinsey", "600", "35%", "100", "25%"))
    assert {p["path"] for p in result["passages"]} == {PRODUCT_GUIDES[locale]}
    for passage, citation in zip(result["passages"], result["citations"], strict=True):
        assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


def test_product_contract_is_shared_with_fallback_generation_and_style():
    assert "commercial hypotheses" in SYSTEM_PROMPT
    assert "Demo cases and test runs" in SYSTEM_PROMPT
    assert "market size or growth" in SYSTEM_PROMPT
    style = (ROOT / "docs" / "ANSWER_STYLE.md").read_text()
    assert 'topic="product"' in style
    assert "willingness to pay" in style

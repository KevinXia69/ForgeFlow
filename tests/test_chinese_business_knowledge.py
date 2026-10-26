"""Chinese definitions must retrieve their sources without inventing case facts."""
from pathlib import Path

import pytest

from forgeflow.knowledge import answer, evidence_lines

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize(("question", "path", "meaning"), [
    ("ForgeFlow 能做什么，聊天能批准执行吗？", "docs/zh/capabilities.md", "聊天保持只读"),
    ("取消采购与当前缺口有什么区别，保留哪些安排？", "docs/zh/quantity-and-documents.md", "当前未覆盖量"),
    ("采购计划的成本和新增采购金额有什么区别？", "docs/zh/cost-and-budget.md", "不是新增采购金额"),
    ("原始验收通过能证明客户已经收到货吗？", "docs/zh/approval-verification-delivery.md", "客户签收"),
    ("摘要没有显示单据，是否说明记录不存在？", "docs/zh/reading-case-evidence.md", "不等于记录不存在"),
])
def test_business_questions_have_relevant_chinese_source(question, path, meaning):
    result = answer(question, ROOT, locale="zh")
    assert result["found"]
    matching = [p for p in result["passages"] if p["path"] == path]
    assert matching and any(meaning in p["text"] for p in matching)
    for citation, passage in zip(result["citations"], result["passages"], strict=True):
        assert not passage["path"].startswith("docs/en/")
        assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


def test_unrelated_chinese_question_does_not_return_deployment_instructions():
    result = answer("量子纠缠蝴蝶面酵母配方", ROOT, locale="zh")
    assert result == {"found": False, "passages": [], "citations": []}


def test_english_sources_remain_separate_after_chinese_documents_added():
    result = answer("Why is chat read-only and when is approval needed?", ROOT, locale="en")
    assert result["found"]
    assert all(p["path"].startswith("docs/en/") for p in result["passages"])

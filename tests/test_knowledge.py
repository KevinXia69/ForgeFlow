"""Tests for deterministic project Q&A with verbatim evidence."""

from __future__ import annotations

from pathlib import Path

import pytest
from support.legacy_index import DocumentIndex

from forgeflow.knowledge import answer, evidence_lines

ROOT = Path(__file__).resolve().parents[1]


def test_read_only_key_403_evidence() -> None:
    result = answer("read-only key api_key.read 403", ROOT, locale="en")
    assert result["found"]
    hit = result["passages"][0]
    assert hit["path"].startswith("docs/en/")
    texts = ["\n".join(evidence_lines(ROOT, citation)) for citation in result["citations"]]
    assert any("api_key.read" in text for text in texts)


def test_hundred_point_run_evidence() -> None:
    result = answer("execution.verifier.score 100.00", ROOT)
    assert result["found"]
    text = "\n".join(
        line for citation in result["citations"] for line in evidence_lines(ROOT, citation)
    )
    assert "execution.verifier.score" in text and "100.00" in text


def test_nonsense_query_returns_nothing() -> None:
    empty = {"found": False, "passages": [], "citations": []}
    assert answer("量子香蕉", ROOT) == empty
    assert DocumentIndex(ROOT).search("量子香蕉") == []
    assert DocumentIndex(ROOT).search("   ") == []


def test_chunk_line_ranges_are_exact() -> None:
    lines = (ROOT / "README.md").read_text(encoding="utf-8").splitlines()
    heading = next(number for number, line in enumerate(lines, 1) if line.startswith("## "))
    passages = [p for p in DocumentIndex(ROOT).passages() if p.path == "README.md"]
    passage = next(p for p in passages if p.start_line == heading)
    assert passage.start_line <= heading <= passage.end_line
    citation = f"README.md:{passage.start_line}-{passage.end_line}"
    assert "\n".join(evidence_lines(ROOT, citation)) == passage.text
    assert all(p.end_line - p.start_line + 1 <= 40 and len(p.text) <= 2000 for p in passages)


@pytest.mark.parametrize(
    "citation",
    ["../etc/passwd:1-2", "/etc/passwd:1-2", "README.md:0-1", "README.md:5-2"],
)
def test_unsafe_citations_raise(citation: str) -> None:
    with pytest.raises(ValueError):
        evidence_lines(ROOT, citation)


def test_symlinked_document_is_skipped(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    target = docs / "real.md"
    target.write_text("# Real\n\nEvidence.", encoding="utf-8")
    (docs / "link.md").symlink_to(target)
    assert {p.path for p in DocumentIndex(tmp_path).passages()} == {"docs/real.md"}


def test_internal_workbench_backlogs_are_not_shipped_capability_evidence(tmp_path: Path) -> None:
    docs = tmp_path / "docs"
    docs.mkdir()
    for name in (
        "ForgeFlow_下一阶段开发方案_v9.md", "ForgeFlow_下一阶段开发方案_v10.md", "V9_实施记录.md",
        "NEXT_PHASE_DEVELOPMENT_PLAN.md", "INVESTIGATION_DEVELOPMENT_PLAN.md",
        "REVIEW_UTILITY_DEVELOPMENT_PLAN.md", "RECORDING_CORRECTIONS.md",
    ):
        (docs / name).write_text("# Proposed\n\nUnimplemented future automatic approvals.")
    (docs / "verified.md").write_text("# Verified\n\nApprovals remain required.")
    assert {p.path for p in DocumentIndex(tmp_path).passages()} == {"docs/verified.md"}


def test_search_is_deterministic_and_respects_limit() -> None:
    index = DocumentIndex(ROOT)
    first = index.search("403 只读 key", limit=3)
    assert first == index.search("403 只读 key", limit=3)
    assert 0 < len(first) <= 3
    assert [score for _, score in first] == sorted((score for _, score in first), reverse=True)


def test_large_file_is_not_indexed(tmp_path: Path) -> None:
    (tmp_path / "big.md").write_bytes(b"x" * (512 * 1024 + 1))
    (tmp_path / "ok.md").write_text("# Ok\n\nSmall document.", encoding="utf-8")
    index = DocumentIndex(tmp_path)
    assert {p.path for p in index.passages()} == {"ok.md"}
    assert index.truncated is False


def test_long_chinese_question_does_not_turn_one_incidental_bigram_into_evidence(tmp_path: Path) -> None:
    (tmp_path / "README.md").write_text("# Evidence\n\n无需为了展示口径再写一次 ERP。只读回放保留原始记录。")
    assert answer("请写一首关于春天的诗。", tmp_path) == {
        "found": False, "passages": [], "citations": []}
    assert answer("只读", tmp_path)["found"]
    assert answer("说明只读回放保留哪些原始记录", tmp_path)["found"]
    assert answer("ERP", tmp_path)["found"]


@pytest.mark.parametrize(("question", "locale", "subject"), [
    ("可以给我介绍一下这个项目吗？", "zh", "计划员"),
    ("这个项目是做什么的？", "zh", "计划员"),
    ("简单介绍一下 ForgeFlow", "zh", "计划员"),
    ("一句话介绍这个工具。", "zh", "计划员"),
    ("用一句话向不懂技术的新同事介绍 ForgeFlow。", "zh", "计划员"),
    ("新来的同事想了解这个工作台。一句话：它能帮什么忙？", "zh", "计划员"),
    ("我想换个话题，向新来的同事介绍一下这个工具。", "zh", "计划员"),
    ("它主要帮谁解决什么问题？两句话概括就好。", "zh", "计划员"),
    ("What does this project do?", "en", "production planners"),
    ("Could you give me an overview of ForgeFlow?", "en", "production planners"),
])
def test_intro_quotes_the_localized_project_overview(question, locale, subject):
    result = answer(question, ROOT, locale=locale)
    passage, = result["passages"]
    citation, = result["citations"]
    assert subject in passage["text"] and "ForgeFlow" in passage["text"]
    assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


@pytest.mark.parametrize("question", [
    "可以给我介绍一下这个项目的成本是怎么计算的吗？",
    "ForgeFlow 里的 plan.new_spend 是什么？",
    "我知道这个工具了，可以给新人介绍一下你怎么计算采购成本吗？",
    "先给我介绍一下项目，不过我主要想知道 plan.new_spend 是什么。",
    "What does ForgeFlow do after approval?",
])
def test_intro_does_not_replace_a_question_about_a_specific_subject(question):
    from forgeflow.knowledge.answer import asks_for_project_overview

    assert not asks_for_project_overview(question)


@pytest.mark.parametrize("lang,question,expected", [
    ("zh", "我第一次打开这里，想看一个例子，应该先点哪里？", "点左侧「案例」"),
    ("en", "I am new. Where should I click to see an example?", "Open “Cases”"),
    ("zh", "这真的能帮工厂省钱吗？目前有什么证据？", "尚未测量"),
    ("en", "How much money has ForgeFlow actually saved?", "has not been measured"),
    ("en", "Can I save money using ForgeFlow?", "has not been measured"),
    ("zh", "原来的安排没留下可比金额，能算出新方案多花多少钱吗？", "差额未知"),
    ("zh", "看到的订单还没付，要怎么核对实际支付？", "付款记录"),
    ("zh", "模拟演示里计划成本便宜五美元，意味着每一次处理都能省钱吗？", "尚未测量"),
    ("zh", "供应商不送货了，谁决定怎么办？", "生产计划员"),
    ("en", "Can we schedule a whole factory with products that share parts?", "multi-level BOM"),
    ("zh", "一句话说说，谁会在什么情况下用到它？", "生产计划员"),
    ("en", "Who would use it and when?", "production planners"),
])
def test_newcomer_questions_prefer_the_relevant_shipping_guidance(lang, question, expected):
    result = answer(question, ROOT, locale=lang)
    first, = result["passages"]
    assert expected in first["text"]
    assert "\n".join(evidence_lines(ROOT, result["citations"][0])) == first["text"]


@pytest.mark.parametrize("lang,question,expected", [
    ("zh", "现在有几件事等我决定？只告诉我数量就好。", "文档里没有当前工作区的实时数量"),
    ("en", "How many cases are awaiting approval?", "The guide has no live workspace counts"),
    ("zh", "轮到我拍板的还有几件？报个数。", "文档里没有当前工作区的实时数量"),
    ("zh", "我手里还有几份方案没签字？", "文档里没有当前工作区的实时数量"),
    ("en", "How many things need my sign-off?", "The guide has no live workspace counts"),
])
def test_static_retrieval_never_supplies_a_live_queue_count(tmp_path, lang, question, expected):
    result = answer(question, ROOT, locale=lang)
    passage, = result["passages"]
    assert expected in passage["text"]
    assert "\n".join(evidence_lines(ROOT, result["citations"][0])) == passage["text"]
    # Old document bundles can have historical counts but lack the new guide section.
    docs = tmp_path / "docs" / "en"
    docs.mkdir(parents=True)
    (docs / "old.md").write_text(f"{question} 17.")
    (tmp_path / "docs" / "old.md").write_text(f"{question} 17.")
    assert not answer(question, tmp_path, locale=lang)["found"]


def test_explaining_approval_steps_does_not_require_a_live_case_count():
    from forgeflow.knowledge.answer import asks_for_workspace_count

    assert not asks_for_workspace_count("If a case is pending, how many approval steps are needed?")
    assert not asks_for_workspace_count("一个待批案例需要几步操作？")
    assert not asks_for_workspace_count("工厂每批货需要几份签字文件？")
    assert not asks_for_workspace_count("How many sign-off steps does each plan require?")

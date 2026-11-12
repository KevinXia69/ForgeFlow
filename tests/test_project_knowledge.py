from pathlib import Path

import pytest
from support.legacy_index import DocumentIndex

from forgeflow.api.app import _home_question_prompt
from forgeflow.knowledge import answer, evidence_lines
from forgeflow.knowledge.guides import TECHNICAL_GUIDES

ROOT = Path(__file__).resolve().parents[1]


def test_uploaded_document_question_uses_scoped_knowledge_and_citations(tmp_path):
    prompt = _home_question_prompt(
        "What is the verification keyword in the uploaded synthetic-fixture.docx?", tmp_path, "en",
    )
    assert "project_knowledge using the user's question" in prompt
    assert "authorized indexed uploads" in prompt
    assert "cite their source" in prompt
    assert "instead of guessing" in prompt


@pytest.mark.parametrize(("locale", "query", "terms"), [
    ("zh", "Agent 架构 DSH Pi 单 Agent", ("DSH", "Pi", "CP-SAT")),
    ("zh", "技术栈 前端 后端 数据库", ("React", "TypeScript", "FastAPI", "PostgreSQL")),
    ("zh", "Agent 框架 LangChain LangGraph", ("DSH", "Pi", "LangGraph")),
    ("zh", "DeepSeek Harness 默认模型 DeepSeek", ("DeepSeek", "配置")),
    # The architecture claim changed with the implementation: retrieval is
    # semantic only, and the guide must say so instead of describing BM25.
    ("zh", "知识库 向量数据库 Embedding RAG", ("Qdrant", "向量", "没有 BM25 分支")),
    ("zh", "MCP stdio 工具 权限", ("MCP", "stdio", "只读")),
    ("en", "technical stack frontend backend database", ("React", "FastAPI", "PostgreSQL")),
    ("en", "agent architecture DSH Pi CP-SAT", ("DSH", "Pi", "CP-SAT")),
    ("en", "RAG embeddings vector database BM25",
     ("Qdrant", "vectors", "no BM25 branch")),
    ("en", "product chat investigation autonomous write permissions", ("DSH", "Pi", "approval")),
])
def test_technical_questions_retrieve_verifiable_project_guide(locale, query, terms):
    result = answer(query, ROOT, locale=locale, source_paths=(TECHNICAL_GUIDES[locale],))
    guide = "docs/TECHNICAL_GUIDE.md" if locale == "zh" else "docs/en/technical-guide.md"
    assert result["found"]
    assert {passage["path"] for passage in result["passages"]} == {guide}
    text = "\n".join(p["text"] for p in result["passages"]).lower()
    for term in terms:
        assert term.lower() in text
    for passage, citation in zip(result["passages"], result["citations"], strict=True):
        assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


@pytest.mark.parametrize("locale", ("zh", "en"))
def test_home_technical_prompt_points_to_project_evidence_not_case_lookup(locale):
    question = "Explain the Agent architecture and technology stack."
    prompt = _home_question_prompt(question, ROOT, locale)
    assert "project_knowledge" in prompt
    assert "technical" in prompt.lower()
    assert "runtime" in prompt.lower()
    assert "default" in prompt.lower()
    assert question in prompt
    assert "No case lookup is needed" in prompt
    assert "case details page, never in chat" in prompt
    assert "Pi permissions must be qualified by profile" in prompt


@pytest.mark.parametrize(("question", "context"), (
    ("我们用了什么技术？", "technical"),
    ("Agent架构是什么？", "technical"),
    ("DSH、Pi 和 CP-SAT 各做什么？", "technical"),
    ("How do Pi investigation and autonomous research differ?", "technical"),
    ("Does MCP allow ERP writes?", "technical"),
    ("Is the planner another LLM?", "technical"),
    ("What embedding or RAG stack is used?", "technical"),
    ("Do you use Kubernetes, Redis or Kafka?", "technical"),
    ("Which model is actually serving this conversation?", "technical"),
    ("不用向量数据库，中英文怎么检索？", "technical"),
    ("React是前端吗？", "technical"),
    ("Explain the front-end and API backend.", "technical"),
    ("用一句话向不懂技术的新同事介绍 ForgeFlow。", "product"),
    ("这个方案影响哪些订单？", "navigation"),
    ("Which orders are shipping before noon?", "navigation"),
    ("What is my capital budget?", "navigation"),
    ("How does ForgeFlow help a production planner?", "product"),
))
def test_technical_context_excludes_business_navigation(tmp_path, question, context):
    guide = tmp_path / "docs" / "WORKBENCH_GUIDE.md"
    guide.parent.mkdir()
    marker = "BUSINESS_NAVIGATION_ONLY_MARKER"
    guide.write_text(marker, encoding="utf-8")
    prompt = _home_question_prompt(question, tmp_path)
    assert (marker in prompt) is (context == "navigation")
    if context == "technical":
        assert "cancelling a supply commitment" not in prompt
        assert "no tools to select a plan" in prompt
    elif context == "product":
        assert prompt.startswith("Answer a ForgeFlow product or market question")


@pytest.mark.parametrize("locale", ("zh", "en"))
def test_architecture_lookup_keeps_investigation_and_autonomous_tool_scope_together(locale):
    result = answer(
        "Agent architecture single agent multi-agent collaboration DSH Pi",
        ROOT, locale=locale, source_paths=(TECHNICAL_GUIDES[locale],),
    )
    guide = "docs/TECHNICAL_GUIDE.md" if locale == "zh" else "docs/en/technical-guide.md"
    assert any(
        passage["path"] == guide
        and "prepare_investigated_recovery" in passage["text"]
        and "execute_recovery" in passage["text"]
        for passage in result["passages"]
    )


@pytest.mark.parametrize("locale", ("zh", "en"))
def test_mcp_documentation_identifies_snapshot_not_chat_erp_credentials(locale):
    result = answer(
        "MCP product chat read-only ERP credentials snapshot", ROOT,
        locale=locale, source_paths=(TECHNICAL_GUIDES[locale],),
    )
    guide = "docs/TECHNICAL_GUIDE.md" if locale == "zh" else "docs/en/technical-guide.md"
    assert any(
        passage["path"] == guide and "snapshot" in passage["text"]
        and "Odoo" in passage["text"]
        for passage in result["passages"]
    )


@pytest.mark.parametrize(("locale", "query"), (
    ("zh", "技术栈 前端 后端 模型 数据库 求解器"),
    ("en", "technical stack frontend backend model database solver"),
))
def test_stack_lookup_keeps_requested_layers_and_model_default_together(locale, query):
    result = answer(query, ROOT, locale=locale, source_paths=(TECHNICAL_GUIDES[locale],))
    assert any(
        all(term in passage["text"] for term in (
            "React", "FastAPI", "PostgreSQL", "DSH", "Pi", "CP-SAT", "DeepSeek", "deepseek-flash",
        ))
        for passage in result["passages"]
    )


def test_source_allowlist_restricts_retrieval_without_changing_default(tmp_path):
    (tmp_path / "docs" / "en").mkdir(parents=True)
    current = tmp_path / "docs" / "en" / "technical-guide.md"
    current.write_text("Current architecture: controlled ERP execution.", encoding="utf-8")
    (tmp_path / "docs" / "en" / "historical.md").write_text(
        "Historical architecture: no ERP execution route.", encoding="utf-8",
    )
    allowed = ("docs/en/technical-guide.md",)
    scoped = answer("architecture ERP execution", tmp_path, locale="en", source_paths=allowed)
    assert {p["path"] for p in scoped["passages"]} == set(allowed)
    ordinary = answer("architecture ERP execution", tmp_path, locale="en")
    assert "docs/en/historical.md" in {p["path"] for p in ordinary["passages"]}


@pytest.mark.parametrize("allowed", (
    (), ("docs/en/missing.md",), ("docs/TECHNICAL_GUIDE.md",), (".runtime/hidden.md",),
))
def test_source_allowlist_cannot_expand_locale_or_fall_back_when_missing(tmp_path, allowed):
    (tmp_path / "docs" / "en").mkdir(parents=True)
    (tmp_path / "docs" / "en" / "technical-guide.md").write_text("Visible architecture.")
    (tmp_path / "docs" / "TECHNICAL_GUIDE.md").write_text("Other locale architecture.")
    (tmp_path / ".runtime").mkdir()
    (tmp_path / ".runtime" / "hidden.md").write_text("Hidden architecture.")
    index = DocumentIndex(tmp_path, locale="en", source_paths=allowed)
    assert index.passages() == []

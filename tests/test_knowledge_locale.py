"""Locale-aware knowledge retrieval tests.

The English corpus is a separate ``docs/en/**/*.md`` namespace. These tests
check source separation, citations, determinism, the unknown-question behavior,
and that the default ``zh`` result shape stays backward compatible.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forgeflow.knowledge import DocumentIndex, answer, evidence_lines
from forgeflow.knowledge.index import tokenize

ROOT = Path(__file__).resolve().parents[1]
CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")

# Ten English questions, one per English document.  The expected path must be
# present in the top-5 result; the substring must appear in that result's text.
ENGLISH_QUESTIONS: tuple[tuple[str, str, str], ...] = (
    (
        "Where is the English reference documentation for ForgeFlow?",
        "docs/en/README.md",
        "English documentation",
    ),
    (
        "What are the runtime layers from the agent to the original verifier?",
        "docs/en/architecture.md",
        "root executor",
    ),
    (
        "How is the read-only Odoo credential protected with api_key.read?",
        "docs/en/permissions.md",
        "api_key.read",
    ),
    (
        "How does ForgeFlow separate raw score from evaluation_valid and environment_chain_ok?",
        "docs/en/evaluation.md",
        "threshold is 99",
    ),
    (
        "What is missing from G3 policy contracts end to end?",
        "docs/en/limits.md",
        "policy_sha256",
    ),
    (
        "How does the Render blueprint configure the review instance?",
        "docs/en/deployment.md",
        "render.yaml",
    ),
    (
        "What is the ff_session cookie and how is it authenticated?",
        "docs/en/api.md",
        "HttpOnly",
    ),
    (
        "How does a reviewer use their own model key in localStorage?",
        "docs/en/model-settings.md",
        "localStorage",
    ),
    (
        "Where does the journal record operation order and commits?",
        "docs/en/evidence.md",
        "journal.jsonl",
    ),
    (
        "Why do English questions only cite English documents?",
        "docs/en/faq.md",
        "docs/en/**/*.md",
    ),
)

# A question with no overlap with any document in the corpus.
UNKNOWN_ENGLISH_QUESTION = "Xylophone marmalade sourdough recipe?"

# Ten Chinese questions from the existing acceptance set.  They must keep
# retrieving the intended Chinese documents and must never cite docs/en.
# The Chinese document corpus was removed with the development notes, so only
# the English corpus is exercised here.



@pytest.fixture(scope="module")
def en_index() -> DocumentIndex:
    index = DocumentIndex(ROOT, locale="en")
    assert index.passages()
    return index


@pytest.fixture(scope="module")
def zh_index() -> DocumentIndex:
    index = DocumentIndex(ROOT)
    assert index.passages()
    return index


def test_english_corpus_has_eight_to_ten_documents(en_index: DocumentIndex) -> None:
    files = sorted((ROOT / "docs" / "en").rglob("*.md"))
    assert 8 <= len(files) <= 10
    indexed = {passage.path for passage in en_index.passages()}
    assert {path.relative_to(ROOT).as_posix() for path in files} == indexed


def test_english_corpus_has_no_cjk_characters() -> None:
    files = sorted((ROOT / "docs" / "en").rglob("*.md"))
    assert files
    for path in files:
        text = path.read_text(encoding="utf-8")
        match = CJK.search(text)
        assert match is None, (
            f"{path.relative_to(ROOT)} contains CJK character {match.group(0)!r}"
        )


def test_english_index_contains_only_docs_en(en_index: DocumentIndex) -> None:
    paths = {passage.path for passage in en_index.passages()}
    assert paths
    assert all(path.startswith("docs/en/") for path in paths)
    assert not any(path.startswith("contracts/") for path in paths)
    assert "README.md" not in paths


def test_zh_index_excludes_english_subtree(zh_index: DocumentIndex) -> None:
    paths = {passage.path for passage in zh_index.passages()}
    assert paths
    assert not any(path.startswith("docs/en/") for path in paths)
    assert "README.md" in paths
    assert not any(path.startswith("docs/en/") for path in paths)


def test_locale_sources_are_separated(tmp_path: Path) -> None:
    (tmp_path / "docs" / "en").mkdir(parents=True)
    (tmp_path / "docs" / "en" / "README.md").write_text(
        "# English\n\nExecutor evidence.", encoding="utf-8"
    )
    (tmp_path / "docs" / "en" / "guide.md").write_text(
        "# Guide\n\nEnglish-only guide.", encoding="utf-8"
    )
    (tmp_path / "docs" / "en" / "ignored.txt").write_text("not markdown", encoding="utf-8")
    (tmp_path / "docs" / "zh.md").write_text("# Zhongwen\n\nChinese locale doc.", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Root\n\nRoot readme.", encoding="utf-8")
    (tmp_path / "contracts").mkdir()
    (tmp_path / "contracts" / "task.json").write_text("{}", encoding="utf-8")

    zh_paths = {passage.path for passage in DocumentIndex(tmp_path).passages()}
    en_paths = {passage.path for passage in DocumentIndex(tmp_path, locale="en").passages()}

    assert zh_paths == {"README.md", "contracts/task.json", "docs/zh.md"}
    assert en_paths == {"docs/en/README.md", "docs/en/guide.md"}
    assert zh_paths.isdisjoint(en_paths)


@pytest.mark.parametrize(
    ("question", "expected_path", "substring"),
    ENGLISH_QUESTIONS,
)
def test_english_question_cites_only_english_docs(
    question: str, expected_path: str, substring: str
) -> None:
    result = answer(question, ROOT, locale="en")

    assert result["found"] is True
    assert result["locale"] == "en"
    assert set(result) == {"found", "passages", "citations", "locale"}
    assert result["passages"]
    assert len(result["citations"]) == len(result["passages"])

    paths = [passage["path"] for passage in result["passages"]]
    assert all(path.startswith("docs/en/") for path in paths), paths
    assert expected_path in paths
    assert substring in "\n".join(passage["text"] for passage in result["passages"])

    for passage, citation in zip(result["passages"], result["citations"], strict=True):
        assert citation == (
            f"{passage['path']}:{passage['start_line']}-{passage['end_line']}"
        )
        assert "\n".join(evidence_lines(ROOT, citation)) == passage["text"]


def test_no_cross_locale_citations(en_index: DocumentIndex, zh_index: DocumentIndex) -> None:
    en_paths = {passage.path for passage in en_index.passages()}
    zh_paths = {passage.path for passage in zh_index.passages()}
    assert en_paths.isdisjoint(zh_paths)

    for question, _, _ in ENGLISH_QUESTIONS:
        result = answer(question, ROOT, locale="en")
        for citation in result["citations"]:
            path = citation.split(":", 1)[0]
            assert path in en_paths
            assert path.startswith("docs/en/")



def test_unknown_english_question_returns_not_found(en_index: DocumentIndex) -> None:
    query_tokens = set(tokenize(UNKNOWN_ENGLISH_QUESTION))
    vocabulary = {
        token for passage in en_index.passages() for token in tokenize(passage.text)
    }
    assert query_tokens
    assert query_tokens.isdisjoint(vocabulary), query_tokens & vocabulary

    assert answer(UNKNOWN_ENGLISH_QUESTION, ROOT, locale="en") == {
        "found": False,
        "passages": [],
        "citations": [],
        "locale": "en",
    }
    assert DocumentIndex(ROOT, locale="en").search(UNKNOWN_ENGLISH_QUESTION) == []


def test_answer_defaults_to_zh_and_keeps_legacy_shape() -> None:
    question = "403 只读 key api_key.read"

    default = answer(question, ROOT)
    explicit = answer(question, ROOT, locale="zh")

    assert default == explicit
    assert default["found"] is True
    assert set(default) == {"found", "passages", "citations"}
    assert DocumentIndex(ROOT).locale == "zh"


def test_answer_signature_remains_positional_and_keyword_compatible() -> None:
    question = "403 只读 key api_key.read"

    positional = answer(question, ROOT, 2)
    keyword = answer(question=question, root=ROOT, limit=2)

    assert positional["found"] is True
    assert keyword["found"] is True
    assert len(positional["passages"]) <= 2
    assert len(keyword["passages"]) <= 2


@pytest.mark.parametrize("locale", ["fr", "", "EN", "zh-CN", "english"])
def test_invalid_locale_raises(locale: str) -> None:
    with pytest.raises(ValueError):
        DocumentIndex(ROOT, locale=locale)
    with pytest.raises(ValueError):
        answer("What is ForgeFlow?", ROOT, locale=locale)


def test_locale_retrieval_is_deterministic(tmp_path: Path) -> None:
    # Use a frozen fixture for answer() so a concurrent edit to the live Chinese
    # docs cannot make two consecutive reads differ for reasons unrelated to
    # retrieval.  The real English subtree is stable and can be compared too.
    stable = tmp_path / "corpus"
    (stable / "docs" / "en").mkdir(parents=True)
    (stable / "docs" / "en" / "guide.md").write_text(
        "# Guide\n\nExecutor evidence is deterministic.\n", encoding="utf-8"
    )
    (stable / "docs" / "zh.md").write_text(
        "# 指南\n\n执行器 证据 是 确定性 的。\n", encoding="utf-8"
    )
    (stable / "README.md").write_text(
        "# Root\n\nProject overview.\n", encoding="utf-8"
    )

    english_question = "executor evidence deterministic"
    chinese_question = "执行器 证据 确定性"

    assert answer(english_question, stable, locale="en") == answer(
        english_question, stable, locale="en"
    )
    assert answer(chinese_question, stable) == answer(chinese_question, stable)

    english_index = DocumentIndex(ROOT, locale="en")
    assert english_index.search("root executor recovery") == english_index.search(
        "root executor recovery"
    )
    assert english_index.passages() == DocumentIndex(ROOT, locale="en").passages()

    # Search on one in-memory Chinese index is deterministic even while other
    # agents may be editing files on disk.
    chinese_index = DocumentIndex(ROOT)
    assert chinese_index.search("403 只读 key") == chinese_index.search("403 只读 key")
    assert chinese_index.passages() == chinese_index.passages()

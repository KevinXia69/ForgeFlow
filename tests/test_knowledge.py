"""Tests for deterministic project Q&A with verbatim evidence."""

from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.knowledge import DocumentIndex, answer, evidence_lines

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
    heading = lines.index("## Boundaries") + 1
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
    for name in ("ForgeFlow_下一阶段开发方案_v9.md", "ForgeFlow_下一阶段开发方案_v10.md", "V9_实施记录.md", "NEXT_PHASE_DEVELOPMENT_PLAN.md", "RECORDING_CORRECTIONS.md"):
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

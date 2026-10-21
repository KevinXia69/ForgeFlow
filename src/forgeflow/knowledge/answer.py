"""Question answering backed only by verbatim, line-cited project passages."""

from __future__ import annotations

import re
from pathlib import Path

from .index import DocumentIndex, tokenize

SCORE_FLOOR = 0.5
_CITATION = re.compile(r"([^:\n]+):(\d+)-(\d+)")
_CJK_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


def _supported_overlap(query_tokens: set[str], passage: str) -> bool:
    # One incidental Chinese bigram in a long question (e.g. 写一) is not
    # enough evidence, even if its rarity gives it a high BM25 score. Keep
    # short lookup queries and exact ASCII identifiers usable.
    cjk = {token for token in query_tokens if _CJK_TOKEN.fullmatch(token)}
    matched = query_tokens.intersection(tokenize(passage))
    return len(cjk) < 4 or len(matched) >= 2 or bool(matched - cjk)


def answer(
    question: str,
    root: str | Path,
    limit: int = 5,
    locale: str = "zh",
) -> dict:
    """Retrieve cited passages above the score floor with sufficient overlap.

    ``locale`` is forwarded to :class:`DocumentIndex`.  The default ``zh`` keeps
    the historical three-key result exactly (``found``, ``passages``,
    ``citations``) so existing callers and exact-key checks are unaffected.
    Non-default locales add ``"locale"`` to the result.
    """
    index = DocumentIndex(Path(root), locale=locale)
    empty = {"found": False, "passages": [], "citations": []}
    if locale != "zh":
        empty["locale"] = locale
    query_tokens = set(tokenize(question))
    if not query_tokens:
        return empty
    hits = [(p, s) for p, s in index.search(question, limit)
            if s > SCORE_FLOOR and _supported_overlap(query_tokens, p.text)]
    if not hits:
        return empty
    result = {
        "found": True,
        "passages": [
            {
                "path": passage.path,
                "start_line": passage.start_line,
                "end_line": passage.end_line,
                "text": passage.text,
                "score": score,
            }
            for passage, score in hits
        ],
        "citations": [f"{p.path}:{p.start_line}-{p.end_line}" for p, _ in hits],
    }
    if locale != "zh":
        result["locale"] = locale
    return result


def evidence_lines(root: str | Path, citation: str) -> list[str]:
    """Resolve ``path:start-end`` under ``root`` and return those exact lines."""
    match = _CITATION.fullmatch(citation.strip())
    if not match:
        raise ValueError(f"malformed citation: {citation!r}")
    raw, first, last = match.group(1), int(match.group(2)), int(match.group(3))
    if first < 1 or last < first:
        raise ValueError(f"invalid line range: {citation!r}")
    relative = Path(raw)
    if relative.is_absolute() or ".." in relative.parts or "." in relative.parts:
        raise ValueError(f"unsafe citation path: {raw!r}")
    base = Path(root).resolve()
    current = base
    for part in relative.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError(f"unsafe citation path: {raw!r}")
    if not current.is_file() or not current.resolve().is_relative_to(base):
        raise ValueError(f"unsafe citation path: {raw!r}")
    try:
        lines = current.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ValueError(f"cannot read citation path: {raw!r}") from exc
    if last > len(lines):
        raise ValueError(f"line range out of bounds: {citation!r}")
    return lines[first - 1 : last]

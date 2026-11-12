"""Question answering backed only by verbatim, line-cited project passages."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Sequence

from .legacy_index import PRODUCT_GUIDES, WORKBENCH_GUIDES, DocumentIndex, Passage, tokenize

_TEST_ADAPTER = None
_TEST_MARKET_ADAPTER = None


SCORE_FLOOR = 0.5
_CITATION = re.compile(r"([^:\n]+):(\d+)-(\d+)")
_CJK_TOKEN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")


# An overview must identify an introduction or audience as the subject. A
# technical question can mention the project and still needs its own sources.
_ZH_SUBJECT = r"(?:forgeflow(?:这个(?:项目|工作台))?|(?:这个|这款|这套)?(?:项目|工作台|工具|系统|软件|产品))"
_ZH_OVERVIEW = re.compile(
    r"(?:请问|请|可以|能否|能不能|能|给我|帮我|简单|简要)*"
    r"(?:(?:给|向|为)[^，。！？,!?.]{0,25}(?:的人|同事|新人|新手|朋友))?"
    rf"(?:(?:介绍|解释|说说|讲讲)(?:一下|下)?(?:{_ZH_SUBJECT}|你(?:自己)?|自己)|"
    rf"{_ZH_SUBJECT}(?:是什么|是(?:用来)?做什么的?|是干什么的?|能(?:帮我)?做什么|有什么用|介绍))(?:呢|吗|吧)?"
)
_EN_SUBJECT = r"(?:forgeflow|(?:this|the) (?:project|workbench|tool|app|system|product))"
_EN_OVERVIEW = re.compile(
    r"(?:(?:can|could|would) you )?(?:please )?(?:briefly )?"
    rf"(?:what(?: is|'s) {_EN_SUBJECT}(?: about| for)?|what (?:does {_EN_SUBJECT} do|can {_EN_SUBJECT} do)|"
    rf"(?:introduce|describe|explain|tell me about|(?:give me )?(?:an? |a brief )?(?:overview|introduction) (?:of|to)) {_EN_SUBJECT})"
)
_AUDIENCE = re.compile(
    r"(?:一句话说说[，,]?)?谁(?:会|可以|通常)?(?:在什么情况[下时]?)?(?:用到|使用)(?:它|这个项目|这个工作台|forgeflow)[呢吗]?"
    r"|(?:它|这个工具|forgeflow)(?:平时|主要|能)?帮谁(?:解决什么(?:问题|麻烦|困难))?[呢吗]?"
    r"|(?:它|这个工具|forgeflow)能帮什么忙[呢吗]?"
    r"|who (?:uses|would use|is) (?:it|forgeflow|this (?:project|workbench))(?: for| and when)?"
)
# A short style instruction can follow the actual question in another sentence.
_ZH_LENGTH_REQUEST = re.compile(
    r"请?用?[一二两三四五六七八九十0-9]+句话?(?:说明|说|讲|介绍|概括|回答)?(?:就好|就行|吧)?"
)
_COUNT = re.compile(
    r"(?:多少|几)(?:件|个|条|笔|项|份)?(?:待批|待审|待处理|待办|事|案|记录|方案|计划|订单|在等|等我)"
    r"|(?:几|多少)(?:个|件|条|笔|项|份)(?=[，。！？,!?]|$)|报个数"
    r"|数量|总数"
    r"|\b(?:how many|number of) (?:of (?:my|the) )?(?:(?:pending|verified|current|open|active) )?"
    r"(?:cases?|items?|things?|decisions?|plans?|tasks?|requests?|records?|runs?|approvals|are (?:there|waiting|pending))\b"
    r"|\b(?:case|decision|pending|approval|task|record|run)s? count\b|\bcount (?:my|the|our|all)\b",
    re.I,
)
_WORKSPACE_QUEUE = re.compile(
    r"待批|待审|待办|(?:待|未|没)(?:我)?签字|[我你](?:要|需)签字|(?:等(?:待)?|轮到|需要|要)[我你](?:决定|批|审|拍板|签字|过目)|待处理|正在执行|已经验收|历史证据|(?:当前|现在|目前|我的|这里|此).{0,5}(?:工作区|工作台|案例)"
    r"|\b(?:pending|awaiting (?:approval|selection)|waiting (?:on|for) (?:me|you|my|a decision|approval)|in progress|"
    r"verified cases|my (?:cases|workspace|sign[- ]off|approval|queue)|this workspace|cases (?:are there|do I have)|evidence records)\b", re.I
)
_OVERVIEW_SECTIONS = {
    "zh": ("docs/CURRENT_CAPABILITIES.md", "## ForgeFlow 是什么？"),
    "en": ("docs/en/current-capabilities.md", "## What is ForgeFlow?"),
}
_MARKET_SECTIONS = {
    "zh": (
        "## 市场需求有什么可核查的依据？",
        "## 市场前景：机会、阻力与结论",
        "## ForgeFlow 是什么？",
        "## 怎样验证真实需求和商业前景？",
    ),
    "en": (
        "## What supports market demand?",
        "## Market outlook: opportunity, barriers and conclusion",
        "## What is ForgeFlow?",
        "## How should demand and commercial prospects be validated?",
    ),
}


def asks_for_project_overview(question: str) -> bool:
    words = " ".join(question.casefold().strip(" 。！？!?.\n\t").split())
    if _EN_OVERVIEW.fullmatch(words) or _AUDIENCE.fullmatch(words):
        return True
    # Visitors often give context first or explicitly change the subject. Match
    # the final request, never a product mention inside a technical question.
    clauses = re.split(r"[，。！？,!?]+", words.replace(" ", ""))
    if len(clauses) > 1 and _ZH_LENGTH_REQUEST.fullmatch(clauses[-1]):
        clauses.pop()
    request = re.sub(r"^请?(?:用)?[一二两三四五六七八九十0-9]+句话[：:]?", "", clauses[-1])
    return bool(_ZH_OVERVIEW.fullmatch(request) or _AUDIENCE.fullmatch(request))


def asks_for_workspace_count(question: str) -> bool:
    return bool(_COUNT.search(question) and _WORKSPACE_QUEUE.search(question))


def _section_passage(index: DocumentIndex, path: str, heading: str) -> Passage | None:
    source = (p for p in index.passages() if p.path == path)
    if any(p.text == heading for p in source):
        paragraph = next(source, None)
        if paragraph and not paragraph.text.startswith("#"):
            return paragraph
    return None


def _supported_overlap(query_tokens: set[str], passage: str) -> bool:
    # One incidental Chinese bigram in a long question (e.g. 写一) is not
    # enough evidence, even if its rarity gives it a high BM25 score. Keep
    # short lookup queries and exact ASCII identifiers usable.
    cjk = {token for token in query_tokens if _CJK_TOKEN.fullmatch(token)}
    matched = query_tokens.intersection(tokenize(passage))
    return len(cjk) < 4 or len(matched) >= 2 or bool(matched - cjk)


def _legacy_answer(
    question: str,
    root: str | Path,
    limit: int = 5,
    locale: str = "zh",
    *,
    source_paths: Sequence[str] | None = None,
    scope=None,
) -> dict:
    """Test-only deterministic retrieval; never selected in production."""
    index = DocumentIndex(Path(root), locale=locale, source_paths=source_paths)
    empty = {"found": False, "passages": [], "citations": []}
    if locale != "zh":
        empty["locale"] = locale
    query_tokens = set(tokenize(question))
    if not query_tokens:
        return empty
    section = None
    live_count = asks_for_workspace_count(question)
    if live_count:
        heading = "## Current workspace counts" if locale == "en" else "## 查看当前工作区数量"
        section = (WORKBENCH_GUIDES[locale], heading)
    elif asks_for_project_overview(question):
        section = _OVERVIEW_SECTIONS[locale]
        if source_paths is not None and PRODUCT_GUIDES[locale] in source_paths:
            section = (PRODUCT_GUIDES[locale], section[1])
    passage = _section_passage(index, *section) if section and limit > 0 else None
    if passage is not None:
        hits = [(passage, 1.0)]
    elif live_count:
        # A static guide can direct readers to the live cards. Historical
        # examples and plans cannot answer a request for the current count.
        return empty
    else:
        hits = [(p, s) for p, s in index.search(question, limit)
                if s > SCORE_FLOOR and _supported_overlap(query_tokens, p.text)]
        # When the workbench guide directly answers the question, avoid
        # appending weak matches from engineering notes to the offline reply.
        if (len(hits) > 1 and hits[0][0].path == WORKBENCH_GUIDES[locale]
                and hits[0][1] >= 2 * hits[1][1]):
            hits = hits[:1]
    if not hits:
        return empty
    return _passage_result(hits, locale)


def _legacy_market_answer(root: str | Path, locale: str = "zh", limit: int = 5,
                          scope=None) -> dict:
    """Test-only deterministic market packet; never selected in production."""
    index = DocumentIndex(Path(root), locale=locale, source_paths=tuple(PRODUCT_GUIDES.values()))
    hits = []
    for heading in _MARKET_SECTIONS[locale][:max(0, limit)]:
        passage = _section_passage(index, PRODUCT_GUIDES[locale], heading)
        if passage is not None:
            hits.append((passage, 1.0))
    return _passage_result(hits, locale)


def _passage_result(hits: Sequence[tuple[Passage, float]], locale: str) -> dict:
    result = {
        "found": bool(hits),
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

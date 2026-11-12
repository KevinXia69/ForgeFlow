"""Keep chat answers prose-only; evidence stays in the evidence views."""
from __future__ import annotations

import re

_DETAILS = re.compile(r"<details\b[^>]*>[\s\S]*?(?:</details>|$)", re.I)
_VIEW_EVIDENCE = re.compile(r"<view\s+evidence\b[^>]*>[\s\S]*?(?:</view>|$)", re.I)
_TAG = re.compile(r"</?(?:details|summary|view)\b[^>]*>", re.I)
_EVIDENCE_HEADING = re.compile(
    r"(?m)^[ \t]*(?:\*\*)?(?:#{1,6}[ \t]*)?"
    r"(?:证据|参考资料|参考|引用|来源|Evidence|References?|Sources?)"
    r"[ \t]*[:：][^\n]*",
    re.I,
)
_SUBSTANTIVE = re.compile(
    r"结论|风险|注意|限制|但|然而|不过|尚未|未批准|未执行|不能|无法|不是|"
    r"实际|金额|conclusion|risk|caveat|but\b|however|not\b|pending|"
    r"unapproved|unexecuted|actual",
    re.I,
)


def _strip_plain_evidence(text: str) -> str:
    """Remove only a trailing, blank-line-separated evidence appendix.

    A heading such as "来源：" can be legitimate prose, especially at the start
    of an answer or when it is followed by a conclusion, a risk/caveat or an
    amount. Only a separated trailing block with no substantive continuation is
    treated as an appendix.
    """
    for match in reversed(list(_EVIDENCE_HEADING.finditer(text))):
        before = text[:match.start()]
        if not re.search(r"\n[ \t]*\n$", before):
            continue
        suffix = match.group(0) + text[match.end():]
        if _SUBSTANTIVE.search(suffix):
            continue
        return before.rstrip()
    return text


def clean_answer(text: str) -> str:
    """Remove model-authored evidence appendices and their raw HTML wrappers."""
    if not isinstance(text, str):
        return ""
    cleaned = _DETAILS.sub("", text)
    cleaned = _VIEW_EVIDENCE.sub("", cleaned)
    cleaned = _TAG.sub("", cleaned)
    cleaned = _strip_plain_evidence(cleaned)
    # A wrapper can leave blank lines behind; keep intentional paragraphs only.
    return re.sub(r"\n[ \t]*\n(?:[ \t]*\n)+", "\n\n", cleaned).strip()

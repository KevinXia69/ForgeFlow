"""Deterministic offline index over project Markdown and contract files."""

from __future__ import annotations

import math
import os
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_PASSAGES = 5000
MAX_LINES = 40
MAX_CHARS = 2000
K1, B = 1.2, 0.75
SUPPORTED_LOCALES = ("zh", "en")
_SKIP_PARTS = frozenset({".git", "runs", "vendor", ".runtime", "node_modules"})
_SKIP_ROOT_PREFIXES = ("ForgeFlow_下一阶段方案", "ForgeFlow_下一阶段计划", "ForgeFlow_下一阶段开发方案_v8")
# Internal development backlogs are proposals, not evidence of shipped product
# capabilities. Keep them out of the user-facing retrieval corpus.
_SKIP_DOC_PREFIXES = ("ForgeFlow_下一阶段", "V9_", "V10_")
_SKIP_DOC_NAMES = frozenset({
    "LOCAL_WORKBENCH.md",
    "访谈提纲_鼎捷.md",
    "局限与边界.md",
    "PUBLIC_REPO.md",
    "30秒演示脚本.md",
    "ACTIVITY_TRACE.md",
    "UI_BENCHMARK_AND_REDESIGN.md",
    "市场与开源对标研究.md",
    "对标研究计划.md",
    "国内赛与黑客松提交要求对照.md",
    "NEXT_PHASE_DSH_REUSE.md",
})
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]+")
_HEADING = re.compile(r"^#{1,6}\s")
# A rule ratio such as 48/51 is one term. Splitting it into "48" and "51" makes
# both halves look common and loses the only part of the query that is rare.
_RATIO = re.compile(r"\d+/\d+")
# Evidence and research notes are build logs: they describe how a change was
# checked, not what the product does. For a content question they must not
# outrank the documents that state the facts, so they carry a lower weight.
_CHANGELOG_PREFIXES = ("docs/evidence/", "docs/research/")
_CHANGELOG_WEIGHT = 0.5


@dataclass(frozen=True)
class Passage:
    """A verbatim, line-addressed slice of a project document."""

    path: str
    start_line: int
    end_line: int
    text: str


def tokenize(text: str) -> list[str]:
    """Return lowercase ASCII words of length >= 2 plus overlapping CJK bigrams."""
    lowered = text.lower()
    tokens = [word for word in _WORD.findall(lowered) if len(word) >= 2]
    tokens.extend(_RATIO.findall(lowered))
    for run in _CJK.findall(text):
        tokens.extend(run[index : index + 2] for index in range(len(run) - 1))
    return tokens


def _checked_locale(locale: str) -> str:
    if locale not in SUPPORTED_LOCALES:
        raise ValueError(
            f"unsupported knowledge locale: {locale!r}; expected one of {SUPPORTED_LOCALES}"
        )
    return locale


def _walk(base: Path, suffixes: tuple[str, ...]) -> list[Path]:
    found: list[Path] = []
    for directory, dirnames, filenames in os.walk(base, followlinks=False):
        dirnames[:] = sorted(
            name
            for name in dirnames
            if name not in _SKIP_PARTS and not (Path(directory) / name).is_symlink()
        )
        found.extend(
            Path(directory) / name
            for name in sorted(filenames)
            if name.endswith(suffixes) and not (Path(directory) / name).is_symlink()
        )
    return found


def _chunks(path: str, lines: list[str]) -> list[Passage]:
    chunks: list[Passage] = []
    current: list[str] = []
    start = 0
    length = 0

    def flush() -> None:
        nonlocal current, start, length
        if current:
            chunks.append(Passage(path, start, start + len(current) - 1, "\n".join(current)))
        current, length = [], 0

    for number, line in enumerate(lines, 1):
        if not line.strip():
            flush()
            continue
        if _HEADING.match(line):
            flush()
        pieces = [line] if len(line) <= MAX_CHARS else [
            line[index : index + MAX_CHARS] for index in range(0, len(line), MAX_CHARS)
        ]
        for piece in pieces:
            if current and (len(current) >= MAX_LINES or length + len(piece) + 1 > MAX_CHARS):
                flush()
            if not current:
                start = number
            current.append(piece)
            length += len(piece) + (1 if len(current) > 1 else 0)
            if len(current) >= MAX_LINES or length >= MAX_CHARS:
                flush()
    flush()
    return chunks


def _inside(root: Path, path: Path) -> bool:
    try:
        path.resolve().relative_to(root)
    except (OSError, ValueError):
        return False
    return True


class DocumentIndex:
    """Index project documents with byte/passage caps and BM25 lookup.

    ``locale="zh"`` keeps the original corpus: ``docs/**/*.md``, root ``*.md``,
    and ``contracts/*.json|*.md``, minus the internal next-stage plans and the
    ``docs/en`` subtree.  ``locale="en"`` is restricted to
    ``docs/en/**/*.md``.  ``extra_paths`` remains available to the ``zh``
    locale; the English locale intentionally ignores it so it cannot index a
    non-English document.
    """

    def __init__(
        self,
        root: Path,
        extra_paths: Sequence[Path] = (),
        locale: str = "zh",
    ) -> None:
        self.root = Path(root).resolve()
        self.locale = _checked_locale(locale)
        self.extra_paths = tuple(Path(path) for path in extra_paths)
        self.truncated = False
        self._passages: list[Passage] = []
        total_bytes = 0
        for relative, path in self._files():
            if len(self._passages) >= MAX_PASSAGES:
                self.truncated = True
                break
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if size > MAX_FILE_BYTES:
                continue
            if total_bytes + size > MAX_TOTAL_BYTES:
                self.truncated = True
                break
            try:
                lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
            except OSError:
                continue
            total_bytes += size
            for passage in _chunks(relative, lines):
                if len(self._passages) >= MAX_PASSAGES:
                    self.truncated = True
                    break
                self._passages.append(passage)
            if self.truncated:
                break
        self._tf = [Counter(tokenize(passage.text)) for passage in self._passages]
        self._dl = [sum(counts.values()) for counts in self._tf]
        self._df: Counter[str] = Counter()
        for counts in self._tf:
            self._df.update(counts.keys())
        self._avgdl = sum(self._dl) / len(self._dl) if self._dl else 0.0

    def _files(self) -> list[tuple[str, Path]]:
        candidates = (
            self._english_candidates()
            if self.locale == "en"
            else self._chinese_candidates()
        )
        chosen: dict[str, Path] = {}
        for path in candidates:
            if path.is_symlink() or not path.is_file() or not _inside(self.root, path):
                continue
            try:
                relative = path.relative_to(self.root)
            except ValueError:
                continue
            if any(part in _SKIP_PARTS for part in relative.parts):
                continue
            if relative.parent == Path(".") and relative.name.startswith(_SKIP_ROOT_PREFIXES):
                continue
            if relative.parent == Path("docs") and relative.name in _SKIP_DOC_NAMES:
                continue
            if relative.parts[0] == "docs" and relative.name.startswith(_SKIP_DOC_PREFIXES):
                continue
            if self.locale == "zh" and relative.parts[:2] == ("docs", "en"):
                continue
            chosen[relative.as_posix()] = path
        return sorted(chosen.items())

    def _english_candidates(self) -> list[Path]:
        english = self.root / "docs" / "en"
        if not english.is_dir() or english.is_symlink():
            return []
        return _walk(english, (".md",))

    def _chinese_candidates(self) -> list[Path]:
        candidates: list[Path] = []
        docs = self.root / "docs"
        if docs.is_dir() and not docs.is_symlink():
            candidates.extend(_walk(docs, (".md",)))
        if self.root.is_dir():
            candidates.extend(sorted(self.root.glob("*.md")))
        contracts = self.root / "contracts"
        if contracts.is_dir() and not contracts.is_symlink():
            candidates.extend(sorted(contracts.glob("*.json")))
            candidates.extend(sorted(contracts.glob("*.md")))
        for extra in self.extra_paths:
            path = extra if extra.is_absolute() else self.root / extra
            if path.is_symlink():
                continue
            if path.is_dir():
                if _inside(self.root, path):
                    candidates.extend(_walk(path, (".md", ".json")))
            else:
                candidates.append(path)
        return candidates

    def passages(self) -> list[Passage]:
        """Return the indexed passages in deterministic path/line order."""
        return list(self._passages)

    def search(self, query: str, limit: int = 5) -> list[tuple[Passage, float]]:
        """Return at most ``limit`` positive-scoring passages, best first."""
        tokens = sorted(set(tokenize(query)))
        if not tokens or limit <= 0 or not self._passages:
            return []
        total = len(self._passages)
        scored: list[tuple[Passage, float]] = []
        for passage, counts, length in zip(self._passages, self._tf, self._dl, strict=True):
            score = 0.0
            for token in tokens:
                frequency = counts.get(token, 0)
                if not frequency:
                    continue
                df = self._df[token]
                idf = math.log(1.0 + (total - df + 0.5) / (df + 0.5))
                norm = frequency + K1 * (1.0 - B + B * length / (self._avgdl or 1.0))
                score += idf * frequency * (K1 + 1.0) / norm
            if score > 0.0:
                for prefix in _CHANGELOG_PREFIXES:
                    if passage.path.startswith(prefix):
                        score *= _CHANGELOG_WEIGHT
                        break
                scored.append((passage, score))
        scored.sort(key=lambda item: (-item[1], item[0].path, item[0].start_line))
        return scored[:limit]

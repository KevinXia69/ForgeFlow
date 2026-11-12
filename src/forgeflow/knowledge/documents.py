"""Bounded enterprise document parsing with citable structure.

Parsing produces a *document block tree*, not one flattened string.  A citation
therefore points at something a person can open: a PDF page and line, a Word
paragraph or table cell, an Excel sheet and cell range, a CSV row and field.

Bounded processing is honest.  A document that hit a limit comes back as
``partial`` with the unparsed scope recorded, and a scanned PDF with no
extractable text comes back as ``needs_review`` instead of as an empty "complete"
document.  Nothing here executes embedded content or macros.
"""
from __future__ import annotations

import csv
import hashlib
import io
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

MAX_BYTES = 20 * 1024 * 1024
MAX_TEXT_CHARS = 2_000_000
MAX_ROWS = 5000
MAX_COLUMNS = 200
MAX_BLOCKS = 200_000
#: A PDF page with fewer extractable characters than this is treated as scanned.
MIN_EXTRACTABLE_PAGE_CHARS = 12

PARSE_COMPLETE = "complete"
PARSE_PARTIAL = "partial"
PARSE_NEEDS_REVIEW = "needs_review"
PARSE_FAILED = "failed"

BLOCK_TYPES = frozenset({"section", "heading", "paragraph", "table", "row", "cell",
                         "line", "page"})
CLASSIFICATIONS = frozenset({
    "enterprise_document", "contract", "sop", "research_note",
    "project_plan", "verified_capability",
})


class DocumentError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class DocumentBlock:
    """One citable unit of a document."""

    ord: int
    block_type: str
    text: str
    locator: str
    parent_ord: int | None = None
    section_path: str | None = None
    page: int | None = None
    paragraph_index: int | None = None
    table_index: int | None = None
    row_index: int | None = None
    cell_ref: str | None = None
    sheet: str | None = None
    cell_range: str | None = None
    line_start: int | None = None
    line_end: int | None = None

    def __post_init__(self) -> None:
        if self.block_type not in BLOCK_TYPES:
            raise DocumentError(f"document_block_type_invalid:{self.block_type}")
        if not self.locator:
            raise DocumentError("document_block_locator_required")

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    filename: str
    kind: str
    text: str
    blocks: tuple[DocumentBlock, ...] = ()
    page_count: int = 0
    sheet_count: int = 0
    row_count: int = 0
    parse_status: str = PARSE_COMPLETE
    unparsed_scope: tuple[str, ...] = ()
    review_state: str = "not_required"
    detail: dict = field(default_factory=dict)
    source_kind: str = "upload"
    classification: str = "enterprise_document"

    @property
    def complete(self) -> bool:
        return self.parse_status == PARSE_COMPLETE

    def sections(self) -> list[DocumentBlock]:
        return [block for block in self.blocks if block.block_type in {"section", "heading"}]

    def citations(self) -> list[dict]:
        return [{"locator": block.locator, "text": block.text[:280],
                 "block_type": block.block_type, "page": block.page,
                 "sheet": block.sheet, "cell_range": block.cell_range}
                for block in self.blocks]


def _decode_text(raw: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise DocumentError("document_unreadable")


def _bounded(text: str) -> str:
    return text[:MAX_TEXT_CHARS]


def classify(filename: str) -> str:
    name = filename.casefold()
    if any(token in name for token in ("contract", "协议", "合同")):
        return "contract"
    if any(token in name for token in ("sop", "procedure", "作业指导", "流程")):
        return "sop"
    if any(token in name for token in ("plan", "开发方案", "计划")):
        return "project_plan"
    if any(token in name for token in ("research", "研究", "笔记", "notes")):
        return "research_note"
    if any(token in name for token in ("verified", "已验证", "capabilit")):
        return "verified_capability"
    return "enterprise_document"


def parse_document(path: str | Path, *, classification: str | None = None,
                   source_kind: str = "upload") -> ParsedDocument:
    source = Path(path)
    try:
        raw = source.read_bytes()
    except OSError:
        raise DocumentError("document_unreadable") from None
    if len(raw) > MAX_BYTES:
        raise DocumentError("document_too_large")
    suffix = source.suffix.lower()
    resolved_classification = classification or classify(source.name)
    if resolved_classification not in CLASSIFICATIONS:
        raise DocumentError("document_classification_invalid")
    kwargs = {"classification": resolved_classification, "source_kind": source_kind}
    if suffix == ".pdf":
        return _pdf(source.name, raw, **kwargs)
    if suffix == ".docx":
        return _docx(source.name, raw, **kwargs)
    if suffix == ".xlsx":
        return _xlsx(source.name, raw, **kwargs)
    if suffix in {".csv", ".tsv"}:
        return _csv(source.name, raw, "\t" if suffix == ".tsv" else ",", **kwargs)
    if suffix in {".md", ".markdown", ".txt"}:
        return _plain(source.name, suffix.lstrip("."), _decode_text(raw), **kwargs)
    raise DocumentError("document_type_unsupported")


def _collect(builder: "_BlockBuilder") -> tuple[tuple[DocumentBlock, ...], str, tuple[str, ...]]:
    blocks, status, scope = builder.finish()
    return tuple(blocks), status, scope


class _BlockBuilder:
    """Collects blocks in order and remembers why a parse could not finish."""

    def __init__(self) -> None:
        self.blocks: list[DocumentBlock] = []
        self.truncated: list[str] = []
        self.chars = 0

    def add(self, block_type: str, text: str, locator: str, **extra) -> DocumentBlock | None:
        if self.chars + len(text) > MAX_TEXT_CHARS:
            self.truncated.append(locator)
            return None
        block = DocumentBlock(ord=len(self.blocks), block_type=block_type, text=text,
                             locator=locator, **extra)
        self.blocks.append(block)
        self.chars += len(text)
        return block

    def finish(self) -> tuple[list[DocumentBlock], str, tuple[str, ...]]:
        status = PARSE_PARTIAL if self.truncated and self.blocks else (
            PARSE_FAILED if not self.blocks else PARSE_COMPLETE)
        return self.blocks, status, tuple(self.truncated)


def _pdf(filename: str, raw: bytes, **kwargs) -> ParsedDocument:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(raw))
        if reader.is_encrypted:
            raise DocumentError("document_encrypted")
        pages = [page.extract_text() or "" for page in reader.pages]
    except DocumentError:
        raise
    except Exception:
        raise DocumentError("document_unreadable") from None
    builder = _BlockBuilder()
    empty_pages: list[str] = []
    for page_number, page_text in enumerate(pages, 1):
        cleaned = page_text.strip()
        section = builder.add("page", cleaned, f"pdf:p{page_number}", page=page_number)
        if len(cleaned) < MIN_EXTRACTABLE_PAGE_CHARS:
            empty_pages.append(f"pdf:p{page_number}")
            continue
        for offset, line in enumerate(cleaned.splitlines(), 1):
            if not line.strip():
                continue
            builder.add("line", line, f"pdf:p{page_number}:l{offset}", page=page_number,
                        line_start=offset, line_end=offset,
                        parent_ord=section.ord if section else None)
    blocks, status, truncated = _collect(builder)
    text = _bounded("\n\n".join(block.text for block in blocks
                                if block.block_type == "page"))
    needs_ocr = bool(pages) and len(empty_pages) == len(pages)
    if needs_ocr:
        status = PARSE_NEEDS_REVIEW
    detail = {"page_count": len(pages), "pages_without_text": empty_pages,
              "truncated_blocks": list(truncated)}
    return ParsedDocument(
        filename, "pdf", text, blocks, page_count=len(pages), parse_status=status,
        unparsed_scope=truncated or (tuple(empty_pages) if needs_ocr else ()),
        review_state="required" if needs_ocr else "not_required",
        detail=detail, **kwargs)


def _docx(filename: str, raw: bytes, **kwargs) -> ParsedDocument:
    from docx import Document

    try:
        document = Document(io.BytesIO(raw))
    except Exception:
        raise DocumentError("document_unreadable") from None
    builder = _BlockBuilder()
    section_path: list[str] = []
    paragraph_index = 0
    table_index = 0
    for item in _docx_in_order(document):
        if getattr(item, "style", None) is not None and str(item.style.name).startswith(
                "Heading"):
            text = item.text.strip()
            if text:
                section_path = [text]
                builder.add("heading", text, f"docx:p{paragraph_index}",
                            paragraph_index=paragraph_index,
                            section_path=" > ".join(section_path))
            paragraph_index += 1
            continue
        if hasattr(item, "rows") and hasattr(item, "columns"):
            table_ref = f"docx:t{table_index}"
            builder.add("table", f"table {table_index}", table_ref, table_index=table_index)
            for row_number, row in enumerate(item.rows):
                cells = [cell.text.strip() for cell in row.cells]
                builder.add("row", "\t".join(cells), f"{table_ref}:r{row_number}",
                            table_index=table_index, row_index=row_number,
                            section_path=" > ".join(section_path) or None)
                for column, value in enumerate(cells):
                    if not value:
                        continue
                    builder.add("cell", value, f"{table_ref}:r{row_number}c{column}",
                                table_index=table_index, row_index=row_number,
                                cell_ref=f"R{row_number}C{column}",
                                section_path=" > ".join(section_path) or None)
            table_index += 1
            continue
        text = item.text.strip()
        if text:
            builder.add("paragraph", text, f"docx:p{paragraph_index}",
                        paragraph_index=paragraph_index,
                        section_path=" > ".join(section_path) or None)
        paragraph_index += 1
    blocks, status, truncated = _collect(builder)
    text = _bounded(_render(blocks))
    return ParsedDocument(filename, "docx", text, blocks, parse_status=status,
                          unparsed_scope=truncated,
                          detail={"truncated_blocks": list(truncated),
                                  "paragraphs": paragraph_index, "tables": table_index},
                          **kwargs)


def _docx_in_order(document) -> Iterator:
    """Yield paragraphs and tables in document order (Word stores them separately)."""
    iter_inner = getattr(document, "iter_inner_content", None)
    if callable(iter_inner):
        yield from iter_inner()
        return
    body = document.element.body
    tags = {body.tag}
    for child in body.iterchildren():
        if child.tag.endswith("}p"):
            from docx.text.paragraph import Paragraph

            yield Paragraph(child, document)
        elif child.tag.endswith("}tbl"):
            from docx.table import Table

            yield Table(child, document)
    del tags


def _xlsx(filename: str, raw: bytes, **kwargs) -> ParsedDocument:
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter

    try:
        workbook = load_workbook(io.BytesIO(raw), read_only=True, data_only=True)
    except Exception:
        raise DocumentError("document_unreadable") from None
    builder = _BlockBuilder()
    rows = 0
    truncated_scope: list[str] = []
    try:
        for sheet in workbook.worksheets:
            builder.add("section", f"# Sheet: {sheet.title}", f"xlsx:{sheet.title}",
                        sheet=sheet.title)
            for row_number, row in enumerate(sheet.iter_rows(values_only=True), 1):
                if rows >= MAX_ROWS:
                    truncated_scope.append(f"xlsx:{sheet.title}:after_r{row_number}")
                    break
                rows += 1
                values = ["" if value is None else str(value) for value in row[:MAX_COLUMNS]]
                if not any(value.strip() for value in values):
                    continue
                if len(row) > MAX_COLUMNS:
                    truncated_scope.append(f"xlsx:{sheet.title}:r{row_number}:columns")
                builder.add("row", "\t".join(values).rstrip(),
                            f"xlsx:{sheet.title}!r{row_number}", sheet=sheet.title,
                            row_index=row_number)
                for column, value in enumerate(values, 1):
                    if not value.strip():
                        continue
                    reference = f"{get_column_letter(column)}{row_number}"
                    builder.add("cell", value, f"xlsx:{sheet.title}!{reference}",
                                sheet=sheet.title, row_index=row_number,
                                cell_ref=reference, cell_range=reference)
    finally:
        workbook.close()
    blocks, status, truncated = _collect(builder)
    scope = tuple(dict.fromkeys([*truncated_scope, *truncated]))
    if truncated_scope:
        status = PARSE_PARTIAL
    text = _bounded(_render(blocks))
    return ParsedDocument(filename, "xlsx", text, blocks, sheet_count=len(workbook.sheetnames),
                          row_count=rows, parse_status=status, unparsed_scope=scope,
                          detail={"truncated_scope": list(scope)}, **kwargs)


def _csv(filename: str, raw: bytes, delimiter: str, **kwargs) -> ParsedDocument:
    text_raw = _decode_text(raw)
    reader = csv.reader(io.StringIO(text_raw), delimiter=delimiter)
    builder = _BlockBuilder()
    rows = 0
    truncated_scope: list[str] = []
    for row in reader:
        rows += 1
        if rows > MAX_ROWS:
            truncated_scope.append(f"csv:after_r{rows - 1}")
            break
        if len(row) > MAX_COLUMNS:
            truncated_scope.append(f"csv:r{rows}:columns")
        builder.add("row", "\t".join(row[:MAX_COLUMNS]), f"csv:r{rows}", row_index=rows)
        for column, value in enumerate(row[:MAX_COLUMNS], 1):
            if not value.strip():
                continue
            builder.add("cell", value, f"csv:r{rows}c{column}", row_index=rows,
                        cell_ref=f"R{rows}C{column}",
                        cell_range=f"r{rows}c{column}")
    blocks, status, truncated = _collect(builder)
    scope = tuple(dict.fromkeys([*truncated_scope, *truncated]))
    if truncated_scope:
        status = PARSE_PARTIAL
    return ParsedDocument(
        filename, "tsv" if delimiter == "\t" else "csv", _bounded(_render(blocks)), blocks,
        row_count=min(rows, MAX_ROWS), parse_status=status, unparsed_scope=scope,
        detail={"truncated_scope": list(scope)}, **kwargs)


def _plain(filename: str, kind: str, text: str, **kwargs) -> ParsedDocument:
    """Markdown and plain text: sections by heading, lines for citation."""
    builder = _BlockBuilder()
    section = ""
    section_ord: int | None = None
    for number, line in enumerate(text.splitlines(), 1):
        if kind in {"md", "markdown"} and line.startswith("#"):
            section = line.lstrip("#").strip()
            block = builder.add("heading", line, f"{kind}:l{number}",
                                section_path=section, line_start=number, line_end=number)
            section_ord = block.ord if block else section_ord
            continue
        if not line.strip():
            continue
        builder.add("line", line, f"{kind}:l{number}", section_path=section or None,
                    line_start=number, line_end=number, parent_ord=section_ord)
    blocks, status, truncated = _collect(builder)
    if status == PARSE_PARTIAL:
        # A plain-text file that could not be fully read is explicitly partial.
        pass
    rendered = "\n".join(block.text for block in blocks)
    return ParsedDocument(filename, kind, _bounded(rendered), blocks, parse_status=status,
                          unparsed_scope=truncated,
                          detail={"truncated_blocks": list(truncated)}, **kwargs)


def _render(blocks: Iterable[DocumentBlock]) -> str:
    """Flatten blocks for display while keeping the block tree for citation."""
    parts: list[str] = []
    for block in blocks:
        if block.block_type in {"cell", "table", "page"}:
            continue
        parts.append(block.text)
    return "\n".join(parts)


def blocks_to_chunks(parsed: ParsedDocument, *, max_chars: int = 1500,
                     max_blocks: int = 40) -> list[dict]:
    """Group citable blocks into retrieval chunks that keep their locators."""
    chunks: list[dict] = []
    buffer: list[DocumentBlock] = []

    def flush() -> None:
        if not buffer:
            return
        text = "\n".join(block.text for block in buffer)
        chunks.append({
            "text": text,
            "locator": buffer[0].locator,
            "locators": [block.locator for block in buffer],
            "section_path": buffer[0].section_path,
            "page": buffer[0].page,
            "sheet": buffer[0].sheet,
            "cell_range": buffer[0].cell_range,
            "line_start": buffer[0].line_start,
            "line_end": buffer[-1].line_end,
        })
        buffer.clear()

    size = 0
    for block in parsed.blocks:
        if block.block_type in {"cell", "table", "page"}:
            continue
        if buffer and (size + len(block.text) > max_chars or len(buffer) >= max_blocks):
            flush()
            size = 0
        buffer.append(block)
        size += len(block.text)
    flush()
    return chunks


_LOCATOR = re.compile(r"^(pdf:p\d+(:l\d+)?|docx:(p\d+|t\d+(:r\d+(c\d+)?)?)|"
                      r"xlsx:.+!(r\d+|[A-Z]+\d+)|csv:r\d+(c\d+)?|(md|txt|markdown):l\d+)$")


def is_valid_locator(locator: str) -> bool:
    return bool(_LOCATOR.match(locator))

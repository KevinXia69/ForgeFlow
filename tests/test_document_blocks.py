"""Citable document structure: locations, ordering and completeness."""
from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.knowledge.documents import (
    DocumentError,
    blocks_to_chunks,
    is_valid_locator,
    parse_document,
)


def test_csv_locators_address_rows_and_fields(tmp_path: Path) -> None:
    path = tmp_path / "orders.csv"
    path.write_text("order,product,qty\nSO1,FIN,3\n", encoding="utf-8")
    parsed = parse_document(path)
    rows = [block for block in parsed.blocks if block.block_type == "row"]
    cells = [block for block in parsed.blocks if block.block_type == "cell"]
    assert [block.locator for block in rows] == ["csv:r1", "csv:r2"]
    assert ("csv:r2c1", "SO1") in {(block.locator, block.text) for block in cells}
    assert parsed.parse_status == "complete"
    assert all(is_valid_locator(block.locator) for block in parsed.blocks)


def test_docx_keeps_document_order_and_names_table_cells(tmp_path: Path) -> None:
    from docx import Document

    document = Document()
    document.add_heading("Receiving", level=1)
    document.add_paragraph("Check the delivery against the purchase order.")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "SO1"
    table.cell(0, 1).text = "late"
    table.cell(1, 0).text = "SO2"
    table.cell(1, 1).text = "on time"
    document.add_paragraph("Escalate a late delivery.")
    path = tmp_path / "sop.docx"
    document.save(path)

    parsed = parse_document(path)
    ordered = [block for block in parsed.blocks
               if block.block_type in {"heading", "paragraph"}]
    assert [block.text for block in ordered] == [
        "Receiving", "Check the delivery against the purchase order.",
        "Escalate a late delivery."]
    assert ordered[0].section_path == "Receiving"
    cells = {(block.locator, block.text) for block in parsed.blocks
             if block.block_type == "cell"}
    assert ("docx:t0:r0c0", "SO1") in cells
    assert ("docx:t0:r1c1", "on time") in cells
    # Paragraph indices address ``document.paragraphs``, which excludes table
    # paragraphs: heading 0, body 1, closing body 2.
    paragraphed = [block.paragraph_index for block in parsed.blocks
                   if block.block_type == "paragraph"]
    assert paragraphed == [1, 2]
    assert [document.paragraphs[index].text for index in paragraphed] == [
        "Check the delivery against the purchase order.",
        "Escalate a late delivery."]


def test_xlsx_names_sheet_and_cell(tmp_path: Path) -> None:
    from openpyxl import Workbook

    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Commitments"
    sheet.append(["order", "qty"])
    sheet.append(["SO1", 3])
    path = tmp_path / "commitments.xlsx"
    workbook.save(path)

    parsed = parse_document(path)
    sheet_block = next(block for block in parsed.blocks if block.block_type == "section")
    assert sheet_block.sheet == "Commitments"
    cell = next(block for block in parsed.blocks
                if block.block_type == "cell" and block.text == "SO1")
    assert cell.locator == "xlsx:Commitments!A2"
    assert cell.cell_range == "A2"
    assert cell.sheet == "Commitments"


def test_pdf_cites_page_and_line(tmp_path: Path) -> None:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=200, height=200)
    path = tmp_path / "blank.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    parsed = parse_document(path)
    page = next(block for block in parsed.blocks if block.block_type == "page")
    assert page.locator == "pdf:p1" and page.page == 1
    # No extractable text is a review state, not a complete document.
    assert parsed.parse_status == "needs_review"
    assert parsed.review_state == "required"
    assert parsed.unparsed_scope == ("pdf:p1",)


def test_markdown_sections_and_line_locators(tmp_path: Path) -> None:
    path = tmp_path / "notes.md"
    path.write_text("# lead time\nSupplier lead time is fourteen days.\n\n# budget\n"
                    "The budget cap is approved.\n", encoding="utf-8")
    parsed = parse_document(path)
    headings = [block for block in parsed.blocks if block.block_type == "heading"]
    assert [block.section_path for block in headings] == ["lead time", "budget"]
    assert headings[0].locator == "md:l1"
    line = next(block for block in parsed.blocks if block.text.startswith("Supplier"))
    assert line.locator == "md:l2" and line.section_path == "lead time"


def test_over_limit_document_is_partial_not_complete(tmp_path: Path, monkeypatch) -> None:
    from forgeflow.knowledge import documents

    monkeypatch.setattr(documents, "MAX_TEXT_CHARS", 40)
    path = tmp_path / "long.md"
    path.write_text("\n".join(f"line {index} of a long document" for index in range(20)),
                    encoding="utf-8")
    parsed = parse_document(path)
    assert parsed.parse_status == "partial"
    assert parsed.complete is False
    assert parsed.unparsed_scope, "the unparsed range must be recorded"


def test_blocks_group_into_citable_chunks(tmp_path: Path) -> None:
    path = tmp_path / "policy.md"
    path.write_text("# policy\n" + "\n".join(f"rule {index}" for index in range(10)),
                    encoding="utf-8")
    parsed = parse_document(path)
    chunks = blocks_to_chunks(parsed, max_chars=60, max_blocks=3)
    assert chunks
    assert all(chunk["text"] for chunk in chunks)
    assert chunks[0]["locator"].startswith("md:l")


def test_encrypted_and_unsupported_documents_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\x00\x01")
    with pytest.raises(DocumentError, match="document_type_unsupported"):
        parse_document(path)
    missing = tmp_path / "missing.md"
    with pytest.raises(DocumentError, match="document_unreadable"):
        parse_document(missing)

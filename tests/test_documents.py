from __future__ import annotations

from pathlib import Path

import pytest

from forgeflow.knowledge.documents import DocumentError, parse_document


def test_csv_and_text_are_bounded(tmp_path: Path) -> None:
    csv_path = tmp_path / "orders.csv"
    csv_path.write_text("order,product,qty\nSO1,FIN,3\n", encoding="utf-8")
    parsed = parse_document(csv_path)
    assert parsed.kind == "csv" and "SO1" in parsed.text and parsed.row_count == 2
    text_path = tmp_path / "notes.md"
    text_path.write_text("supplier lead time", encoding="utf-8")
    assert parse_document(text_path).text == "supplier lead time"


def test_docx_tables_and_paragraphs(tmp_path: Path) -> None:
    from docx import Document
    document = Document()
    document.add_paragraph("Supplier note")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "SO1"
    table.cell(0, 1).text = "late"
    path = tmp_path / "note.docx"
    document.save(path)
    parsed = parse_document(path)
    assert "Supplier note" in parsed.text and "SO1\tlate" in parsed.text


def test_xlsx_sheets(tmp_path: Path) -> None:
    from openpyxl import Workbook
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "Orders"
    sheet.append(["order", "qty"])
    sheet.append(["SO1", 3])
    path = tmp_path / "orders.xlsx"
    workbook.save(path)
    parsed = parse_document(path)
    assert parsed.kind == "xlsx" and parsed.sheet_count == 1
    assert "# Sheet: Orders" in parsed.text and "SO1" in parsed.text


def test_pdf_without_text_is_readable(tmp_path: Path) -> None:
    from pypdf import PdfWriter
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    path = tmp_path / "blank.pdf"
    with path.open("wb") as handle:
        writer.write(handle)
    parsed = parse_document(path)
    assert parsed.kind == "pdf" and parsed.page_count == 1


def test_unsupported_and_oversize_fail_closed(tmp_path: Path) -> None:
    path = tmp_path / "binary.bin"
    path.write_bytes(b"\x00\x01")
    with pytest.raises(DocumentError, match="unsupported"):
        parse_document(path)

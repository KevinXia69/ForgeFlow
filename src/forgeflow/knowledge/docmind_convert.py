"""Deterministic format conversion before a Document Mind submission.

Document Mind accepts PDF/Office/images/Markdown/TXT/HTML directly.  CSV, TSV
and JSON are converted to a Markdown table here, without changing cell values or
business numbers.  The original file, its hash and the conversion version are
returned so the caller can preserve both and cite the conversion.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CONVERSION_VERSION = "docmind-convert-v2"
DIRECT_EXTENSIONS = frozenset({
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "png", "jpg", "jpeg",
    "bmp", "gif", "webp", "md", "markdown", "txt", "html", "htm",
})
TABLE_EXTENSIONS = frozenset({"csv", "tsv"})
JSON_EXTENSION = "json"


class ConversionError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class PreparedSource:
    upload_path: Path
    upload_filename: str
    extension: str
    conversion: dict[str, Any]

    @property
    def converted(self) -> bool:
        return bool(self.conversion.get("converted"))


def prepare(path: str | Path, filename: str, output_dir: str | Path, *,
            target: str = "acs") -> PreparedSource:
    """Prepare a source for the vendor.

    ``target='bailian'`` is the DashScope API-key path: it accepts only
    doc/docx/pdf, so other formats are converted to a faithful PDF/DOCX first.
    ``target='acs'`` accepts the wider set directly and converts only structured
    text to Markdown.
    """
    if target == "bailian":
        return _prepare_bailian(path, filename, output_dir)
    """Return the file to submit; raises ``unsupported_format`` when unsafe.

    Direct formats are not copied: the original path is submitted.  Converted
    formats are written next to the caller-provided output directory with a
    deterministic Markdown representation.
    """
    source = Path(path)
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in DIRECT_EXTENSIONS:
        return PreparedSource(source, Path(filename).name, suffix, {
            "converted": False, "version": CONVERSION_VERSION,
            "original_filename": Path(filename).name})
    if suffix in TABLE_EXTENSIONS:
        markdown = _table_markdown(source.read_bytes(), "\t" if suffix == "tsv" else ",")
        return _write(output_dir, Path(filename).stem + ".md", markdown, suffix,
                      "markdown_table")
    if suffix == JSON_EXTENSION:
        markdown = _json_markdown(source.read_bytes())
        return _write(output_dir, Path(filename).stem + ".md", markdown, suffix,
                      "json_markdown")
    raise ConversionError("unsupported_format")


def _prepare_bailian(path: str | Path, filename: str,
                     output_dir: str | Path) -> PreparedSource:
    source = Path(path)
    suffix = Path(filename).suffix.lower().lstrip(".")
    if suffix in {"pdf", "doc", "docx"}:
        return PreparedSource(source, Path(filename).name, suffix, {
            "converted": False, "version": CONVERSION_VERSION,
            "original_filename": Path(filename).name, "target": "bailian"})
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    if suffix in {"xls", "xlsx", "ppt", "pptx"}:
        target = _soffice_pdf(source, folder)
        return _converted(target, filename, folder, "office_pdf")
    if suffix in {"png", "jpg", "jpeg", "bmp", "gif", "webp"}:
        target = folder / (Path(filename).stem + ".pdf")
        try:
            from PIL import Image
        except ImportError:
            raise ConversionError("image_converter_missing") from None
        Image.open(source).convert("RGB").save(target, "PDF")
        return _converted(target, filename, folder, "image_pdf")
    if suffix in {"csv", "tsv"}:
        workbook = folder / (Path(filename).stem + ".xlsx")
        _table_xlsx(source, workbook, "\t" if suffix == "tsv" else ",")
        target = _soffice_pdf(workbook, folder)
        return _converted(target, filename, folder, "table_xlsx_pdf")
    if suffix == JSON_EXTENSION:
        workbook = folder / (Path(filename).stem + ".xlsx")
        _json_xlsx(source, workbook)
        target = _soffice_pdf(workbook, folder)
        return _converted(target, filename, folder, "json_xlsx_pdf")
    if suffix in {"md", "markdown", "html", "htm", "txt"}:
        target = folder / (Path(filename).stem + ".docx")
        if suffix == "txt":
            _text_docx(source, target)
        else:
            _pandoc_docx(source, target, reader="html+raw_html" if suffix in {"html", "htm"}
                          else "gfm")
        return _converted(target, filename, folder, "text_docx")
    raise ConversionError("unsupported_format")


def _converted(target: Path, filename: str, folder: Path, kind: str) -> PreparedSource:
    if not target.is_file():
        raise ConversionError("conversion_failed")
    return PreparedSource(target, target.name, target.suffix.lstrip("."), {
        "converted": True, "version": CONVERSION_VERSION, "kind": kind,
        "original_filename": Path(filename).name, "target": "bailian",
        "converted_extension": target.suffix.lstrip(".")})


def _soffice_pdf(source: Path, folder: Path) -> Path:
    soffice = shutil.which("soffice") or "/Applications/LibreOffice.app/Contents/MacOS/soffice"
    try:
        subprocess.run([soffice, "--headless", "--convert-to", "pdf",
                        "--outdir", str(folder), str(source)],
                       check=True, timeout=180, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        raise ConversionError("conversion_failed") from None
    return folder / (source.stem + ".pdf")


def _text_docx(source: Path, target: Path) -> None:
    """Keep plain text literal: Markdown markers and line breaks are data."""
    from docx import Document

    text = _decode(source.read_bytes()).replace("\r\n", "\n").replace("\r", "\n")
    document = Document()
    try:
        for line in text.split("\n"):
            document.add_paragraph(line)
        document.save(target)
    except (OSError, ValueError):
        raise ConversionError("conversion_failed") from None


def _without_local_media(text: str) -> str:
    """Pandoc's DOCX writer embeds images; untrusted uploads must not read them."""
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", text)
    text = re.sub(r"<img\b[^>]*>", "", text, flags=re.I)
    text = re.sub(r"\\includegraphics(?:\[[^\]]*\])?\{[^}]*\}", "", text)
    return text


def _pandoc_docx(source: Path, target: Path, *, reader: str) -> None:
    pandoc = shutil.which("pandoc")
    if not pandoc:
        raise ConversionError("converter_missing")
    try:
        reference = Path(os.environ.get("FORGEFLOW_PANDOC_REFERENCE",
                                         "/app/pandoc-reference.docx"))
        with tempfile.TemporaryDirectory(prefix="pandoc-reference-", dir=target.parent) as directory:
            if not reference.is_file():
                # Distro Pandoc packages keep their DOCX data outside the binary.
                # Export only the installed default, never user input, outside
                # the sandbox, then explicitly allow this private reference file.
                reference = Path(directory) / "reference.docx"
                default = subprocess.run(
                    [pandoc, "--print-default-data-file", "reference.docx"],
                    check=True, timeout=30, capture_output=True)
                reference.write_bytes(default.stdout)
            # Debian's Pandoc data package is incompatible with --sandbox's
            # data-file lookup. Keep the conversion isolated by removing media
            # references and giving Pandoc an empty resource path instead.
            resources = Path(directory) / "resources"
            resources.mkdir()
            subprocess.run(
                [pandoc, "--reference-doc", str(reference),
                 "--resource-path", str(resources),
                 "--from", reader, "--to", "docx", "--output", str(target)],
                input=_without_local_media(_decode(source.read_bytes())).encode("utf-8"),
                check=True, timeout=120, capture_output=True)
    except (OSError, subprocess.SubprocessError):
        raise ConversionError("conversion_failed") from None


def _table_xlsx(source: Path, target: Path, delimiter: str) -> None:
    import csv as _csv

    from openpyxl import Workbook

    rows = list(_csv.reader(io.StringIO(_decode(source.read_bytes())),
                            delimiter=delimiter))
    workbook = Workbook()
    sheet = workbook.active
    for row in rows:
        sheet.append([_cell(value) for value in row])
    workbook.save(target)


def _json_rows(value: Any) -> list[tuple[str, Any]]:
    """Flatten every JSON leaf, including all top-level arrays and scalars."""
    rows: list[tuple[str, Any]] = []

    def walk(prefix: str, node: Any) -> None:
        if isinstance(node, dict):
            if not node and prefix:
                rows.append((prefix, "{}"))
                return
            for key, child in node.items():
                path = f"{prefix}.{key}" if prefix else str(key)
                walk(path, child)
            return
        if isinstance(node, list):
            if not node and prefix:
                rows.append((prefix, "[]"))
                return
            for index, child in enumerate(node):
                walk(f"{prefix}[{index}]" if prefix else f"[{index}]", child)
            return
        rows.append((prefix or "value", node))

    walk("", value)
    return rows


def _json_xlsx(source: Path, target: Path) -> None:
    from openpyxl import Workbook

    value = json.loads(_decode(source.read_bytes()))
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "JSON"
    sheet.column_dimensions["A"].width = 120
    for path, leaf in _json_rows(value):
        sheet.append([f"{path} = {_cell(leaf)}"])
    workbook.save(target)


def _write(output_dir: str | Path, name: str, text: str, original_extension: str,
           kind: str) -> PreparedSource:
    folder = Path(output_dir)
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / name
    target.write_text(text, encoding="utf-8")
    return PreparedSource(target, name, "md", {
        "converted": True, "version": CONVERSION_VERSION, "kind": kind,
        "original_extension": original_extension,
        "original_filename": name.replace(".md", "." + original_extension)})


def _table_markdown(raw: bytes, delimiter: str) -> str:
    text = _decode(raw)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [row for row in rows if any(cell.strip() for cell in row)]
    if not rows:
        raise ConversionError("empty_source")
    width = max(len(row) for row in rows)
    rows = [row + [""] * (width - len(row)) for row in rows]
    header = rows[0]
    lines = ["| " + " | ".join(_escape(cell) for cell in header) + " |",
             "| " + " | ".join("---" for _ in header) + " |"]
    lines.extend("| " + " | ".join(_escape(cell) for cell in row) + " |"
                 for row in rows[1:])
    return "\n".join(lines) + "\n"


def _json_markdown(raw: bytes) -> str:
    try:
        value = json.loads(_decode(raw))
    except ValueError:
        raise ConversionError("source_unreadable") from None
    lines = ["```text"]
    for path, leaf in _json_rows(value):
        lines.append(f"{path} = {_cell(leaf)}")
    lines.append("```")
    return "\n".join(lines) + "\n"


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        try:
            return raw.decode("gb18030")
        except UnicodeDecodeError:
            raise ConversionError("source_not_utf8") from None


def _cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    return str(value)


def _escape(value: Any) -> str:
    return _cell(value).replace("|", "\\|").replace("\n", " ")

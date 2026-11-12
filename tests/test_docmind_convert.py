import base64
import json
import shutil
import subprocess
import threading
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from docx import Document
from openpyxl import load_workbook

from forgeflow.knowledge.docmind_convert import ConversionError, _json_markdown, _json_xlsx, prepare


def _payload() -> dict:
    return {
        "budget": 120000,
        "currency": "CNY",
        "orders": [{"id": "SO-1", "qty": 3}],
        "approvals": [{"id": "AP-1", "state": "approved"}],
    }


def test_json_to_markdown_keeps_every_array_and_scalar(tmp_path: Path) -> None:
    text = _json_markdown(json.dumps(_payload()).encode())
    for value in ("budget", "120000", "currency", "CNY", "orders[0].id",
                  "SO-1", "approvals[0].state", "approved"):
        assert value in text


def test_json_to_xlsx_keeps_every_array_and_scalar(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    target = tmp_path / "output.xlsx"
    source.write_text(json.dumps(_payload()), encoding="utf-8")
    _json_xlsx(source, target)
    rows = [str(row[0]) for row in
            load_workbook(target).active.iter_rows(values_only=True) if row[0]]
    text = "\n".join(rows)
    assert "budget = 120000" in text
    assert "currency = CNY" in text
    assert "orders[0].id = SO-1" in text
    assert "approvals[0].state = approved" in text


@pytest.mark.parametrize("encoding", ["utf-8-sig", "gb18030"])
def test_bailian_txt_preserves_chinese_literal_markers_and_lines_without_pandoc(
        tmp_path, monkeypatch, encoding):
    source = tmp_path / "stored-content.txt"
    text = "采购审批：预算 120000 元\n# 这是原文，不是标题\n\n*产品*\tDC24_继电器\n"
    raw = text.encode(encoding)
    source.write_bytes(raw)
    monkeypatch.setattr(shutil, "which", lambda _: None)
    result = prepare(source, "中文资料.txt", tmp_path / "converted", target="bailian")
    assert result.converted and result.extension == "docx"
    assert result.upload_filename == "中文资料.docx"
    assert "\n".join(paragraph.text for paragraph in Document(result.upload_path).paragraphs) == text
    assert source.read_bytes() == raw


@pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc not installed")
@pytest.mark.parametrize("extension", ["md", "markdown", "html", "htm"])
def test_bailian_markup_conversion_preserves_chinese_headings_and_table(tmp_path, extension):
    source = tmp_path / "content-address-without-extension"
    text = ("# 采购审批\n\n金额保持不变。\n\n| 物料 | 数量 |\n| --- | --- |\n| 继电器 | 6 |\n"
            if extension in {"md", "markdown"} else
            "<h1>采购审批</h1><p>金额保持不变。</p><table><tr><th>物料</th><th>数量</th></tr>"
            "<tr><td>继电器</td><td>6</td></tr></table>")
    source.write_text(text, encoding="utf-8")
    result = prepare(source, f"采购规则.{extension}", tmp_path / "out", target="bailian")
    document = Document(result.upload_path)
    assert result.converted and result.extension == "docx"
    assert any(p.text == "采购审批" for p in document.paragraphs)
    assert document.tables[0].cell(1, 0).text == "继电器"
    assert document.tables[0].cell(1, 1).text == "6"


def test_markup_uses_a_trusted_reference_and_strips_media(tmp_path, monkeypatch):
    source = tmp_path / "input.md"
    source.write_text("# Uploaded text\n\n![private](file:///private/image.png)\n")
    calls = []
    references = []
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/pandoc")

    def run(command, **kwargs):
        calls.append(command)
        assert kwargs["check"] and kwargs["capture_output"]
        assert 0 < kwargs["timeout"] <= 120
        if len(calls) == 1:
            assert command == ["/usr/bin/pandoc", "--print-default-data-file", "reference.docx"]
            assert "input" not in kwargs
            return subprocess.CompletedProcess(command, 0, stdout=b"trusted reference")
        assert "--sandbox" not in command
        assert "--resource-path" in command
        assert b"file:///private/image.png" not in kwargs["input"]
        assert kwargs["input"].startswith(b"# Uploaded text")
        reference = Path(command[command.index("--reference-doc") + 1])
        references.append(reference)
        assert reference.read_bytes() == b"trusted reference"
        Document().save(command[command.index("--output") + 1])
        return subprocess.CompletedProcess(command, 0, stdout=b"")

    monkeypatch.setattr(subprocess, "run", run)
    result = prepare(source, "input.md", tmp_path / "out", target="bailian")
    assert result.upload_path.is_file()
    assert len(calls) == 2
    assert references and not references[0].exists()


@pytest.mark.parametrize("failure_call", [1, 2], ids=["reference", "conversion"])
def test_markup_conversion_failure_does_not_retry(
        tmp_path, monkeypatch, failure_call):
    source = tmp_path / "input.md"
    source.write_text("# Uploaded text")
    calls = []
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/pandoc")

    def run(command, **kwargs):
        calls.append(command)
        if len(calls) == failure_call:
            raise subprocess.CalledProcessError(
                1, command, stderr=b"PRIVATE_DIAGNOSTIC_MUST_NOT_LEAK")
        return subprocess.CompletedProcess(command, 0, stdout=b"trusted reference")

    monkeypatch.setattr(subprocess, "run", run)
    with pytest.raises(ConversionError, match="^conversion_failed$"):
        prepare(source, "input.md", tmp_path / "out", target="bailian")
    assert len(calls) == failure_call
    assert all("--sandbox" not in command for command in calls[1:])
    assert list((tmp_path / "out").iterdir()) == []


@pytest.mark.skipif(not shutil.which("pandoc"), reason="pandoc not installed")
@pytest.mark.parametrize("extension", ["md", "html"])
def test_uploaded_markup_cannot_fetch_urls_or_embed_local_files(tmp_path, extension):
    png = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jhfkAAAAASUVORK5CYII=")
    local_image = tmp_path / "private-image.png"
    local_image.write_bytes(png)
    secret = tmp_path / "private.txt"
    secret.write_text("PRIVATE_CONTENT_MUST_NOT_ENTER_DOCX")
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.end_headers()
            self.wfile.write(png)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}/remote.png"
    source = tmp_path / f"upload.{extension}"
    source.write_text(
        f"# Public content\n\n![remote]({url})\n\n![local]({local_image})\n"
        if extension == "md" else
        f'<h1>Public content</h1><img src="{url}"><img src="{local_image.as_uri()}">'
        f'<iframe src="{secret.as_uri()}"></iframe>')
    try:
        result = prepare(source, source.name, tmp_path / "out", target="bailian")
        with zipfile.ZipFile(result.upload_path) as archive:
            assert not any(name.startswith("word/media/") for name in archive.namelist())
            assert b"PRIVATE_CONTENT_MUST_NOT_ENTER_DOCX" not in archive.read("word/document.xml")
        assert requests == []
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)

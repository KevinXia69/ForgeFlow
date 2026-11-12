"""Content-addressed, immutable conversation attachments.

Objects live in a server-selected principal namespace under
``<data_root>/attachments-v1/owners/<principal-sha256>/objects/<prefix>/<sha256>``.
They are written atomically and every read verifies the digest and size. A digest
identifies content, never permission to read another principal's upload. Legacy
unscoped objects have no provable owner and must be uploaded again.
"""
from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

MAX_ATTACHMENT_BYTES = 20 * 1024 * 1024
MAX_EXTRACT_CHARS = 30_000


class AttachmentError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class AttachmentRef:
    digest: str
    name: str
    media_type: str
    size: int


class AttachmentStore:
    def __init__(self, data_root: Path | str, *, principal_id: str) -> None:
        if not isinstance(principal_id, str) or not principal_id.strip():
            raise AttachmentError("attachment_owner_required")
        owner = hashlib.sha256(principal_id.encode("utf-8")).hexdigest()
        self.root = Path(data_root) / "attachments-v1" / "owners" / owner
        self.objects = self.root / "objects"

    @staticmethod
    def _clean_name(name: str) -> str:
        cleaned = Path(name or "attachment").name.replace("\\", "_").strip()
        return cleaned[:200] or "attachment"

    def save(self, data: bytes, *, name: str, media_type: str) -> AttachmentRef:
        if not data:
            raise AttachmentError("attachment_empty")
        if len(data) > MAX_ATTACHMENT_BYTES:
            raise AttachmentError("attachment_too_large")
        digest = hashlib.sha256(data).hexdigest()
        target = self.objects / digest[:2] / digest
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=".upload-")
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(data)
                    handle.flush()
                    os.fsync(handle.fileno())
                try:
                    os.link(tmp, target)
                except FileExistsError:
                    pass
                except OSError:
                    if not target.exists():
                        os.replace(tmp, target)
            finally:
                try:
                    os.unlink(tmp)
                except FileNotFoundError:
                    pass
        return AttachmentRef(digest=digest, name=self._clean_name(name),
                             media_type=(media_type or "application/octet-stream")[:120],
                             size=len(data))

    def resolve(self, ref: AttachmentRef) -> bytes:
        if len(ref.digest) != 64 or any(ch not in "0123456789abcdef" for ch in ref.digest):
            raise AttachmentError("attachment_invalid")
        path = self.objects / ref.digest[:2] / ref.digest
        try:
            data = path.read_bytes()
        except OSError:
            raise AttachmentError("attachment_not_found") from None
        if len(data) != ref.size or hashlib.sha256(data).hexdigest() != ref.digest:
            raise AttachmentError("attachment_corrupt")
        return data

    def host_path(self, ref: AttachmentRef) -> Path:
        self.resolve(ref)
        return self.objects / ref.digest[:2] / ref.digest


def extract_text(data: bytes, name: str) -> tuple[str, bool]:
    """Extract bounded text from a supported file without persisting anything."""
    suffix = Path(name or "upload").suffix.lower()
    try:
        if suffix == ".pdf":
            from pypdf import PdfReader
            text = "\n".join(page.extract_text() or "" for page in PdfReader(BytesIO(data)).pages)
        elif suffix == ".docx":
            from docx import Document
            document = Document(BytesIO(data))
            parts = [p.text for p in document.paragraphs if p.text]
            for table in document.tables:
                for row in table.rows:
                    parts.append("\t".join(cell.text for cell in row.cells))
            text = "\n".join(parts)
        elif suffix == ".xlsx":
            from openpyxl import load_workbook
            book = load_workbook(BytesIO(data), read_only=True, data_only=True)
            parts = []
            for sheet in book.worksheets:
                parts.append(f"[{sheet.title}]")
                for row in sheet.iter_rows(values_only=True):
                    parts.append("\t".join("" if value is None else str(value) for value in row))
            text = "\n".join(parts)
        elif suffix == ".pptx":
            from pptx import Presentation
            parts = []
            for slide in Presentation(BytesIO(data)).slides:
                for shape in slide.shapes:
                    if hasattr(shape, "text") and shape.text:
                        parts.append(str(shape.text))
            text = "\n".join(parts)
        elif suffix in {".csv", ".tsv", ".json", ".md", ".markdown", ".txt"}:
            text = data.decode("utf-8-sig", errors="replace")
        else:
            raise AttachmentError("extract_type_unsupported")
    except AttachmentError:
        raise
    except Exception:
        raise AttachmentError("extract_failed") from None
    return text[:MAX_EXTRACT_CHARS], len(text) > MAX_EXTRACT_CHARS

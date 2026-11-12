import hashlib
from dataclasses import replace
from io import BytesIO

import pytest
from docx import Document

from forgeflow.attachments import AttachmentError, AttachmentStore, extract_text


def test_store_is_content_addressed_and_verifies_tampering(tmp_path):
    store = AttachmentStore(tmp_path, principal_id="owner-a")
    ref = store.save(b"unique attachment bytes", name="../../quote.docx",
                     media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    assert ref.name == "quote.docx"
    assert store.save(b"unique attachment bytes", name="copy.docx", media_type=ref.media_type).digest == ref.digest
    assert store.resolve(ref) == b"unique attachment bytes"
    path = store.host_path(ref)
    path.write_bytes(b"tampered")
    with pytest.raises(AttachmentError) as error:
        store.resolve(ref)
    assert error.value.code == "attachment_corrupt"


def test_same_content_is_private_until_each_owner_uploads_it(tmp_path):
    first = AttachmentStore(tmp_path, principal_id="owner-a")
    second = AttachmentStore(tmp_path, principal_id="owner-b")
    ref = first.save(b"private content", name="note.txt", media_type="text/plain")
    for read in (second.resolve, second.host_path):
        with pytest.raises(AttachmentError, match="attachment_not_found"):
            read(ref)
    other = second.save(b"private content", name="note.txt", media_type="text/plain")
    assert other == ref
    assert first.host_path(ref) != second.host_path(other)
    assert AttachmentStore(tmp_path, principal_id="owner-a").resolve(ref) == b"private content"
    assert second.resolve(other) == b"private content"


def test_legacy_unowned_objects_are_not_adopted_by_reference(tmp_path):
    from forgeflow.attachments import AttachmentRef

    content = b"unowned legacy content"
    digest = hashlib.sha256(content).hexdigest()
    legacy = tmp_path / "attachments-v1" / "objects" / digest[:2] / digest
    legacy.parent.mkdir(parents=True)
    legacy.write_bytes(content)
    store = AttachmentStore(tmp_path, principal_id="owner-a")
    ref = AttachmentRef(digest, "legacy.txt", "text/plain", len(content))
    with pytest.raises(AttachmentError, match="attachment_not_found"):
        store.resolve(ref)
    assert store.save(content, name=ref.name, media_type=ref.media_type) == ref
    assert store.resolve(ref) == content
    assert legacy.read_bytes() == content


@pytest.mark.parametrize("principal", ["", "   ", None])
def test_store_requires_an_owner(tmp_path, principal):
    with pytest.raises(AttachmentError, match="attachment_owner_required"):
        AttachmentStore(tmp_path, principal_id=principal)


def test_size_and_digest_are_verified_inside_owner_namespace(tmp_path):
    store = AttachmentStore(tmp_path, principal_id="../../unsafe-name")
    ref = store.save(b"12345", name="note.txt", media_type="text/plain")
    with pytest.raises(AttachmentError, match="attachment_corrupt"):
        store.resolve(replace(ref, size=4))
    # Same byte length still fails the SHA-256 check.
    path = store.host_path(ref)
    assert path.is_relative_to(tmp_path / "attachments-v1" / "owners")
    path.write_bytes(b"54321")
    with pytest.raises(AttachmentError, match="attachment_corrupt"):
        store.resolve(ref)


def test_extract_docx_includes_tables():
    document = Document()
    document.add_paragraph("Intro")
    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "Amount"
    table.cell(0, 1).text = "SIGNOFF-TABLE-777"
    payload = BytesIO()
    document.save(payload)
    text, truncated = extract_text(payload.getvalue(), "contract.docx")
    assert "Intro" in text
    assert "SIGNOFF-TABLE-777" in text
    assert truncated is False


def test_harness_handle_names_digest_and_read_only_path(tmp_path):
    from forgeflow.api.app import _attachment_handles
    from forgeflow.attachments import AttachmentRef

    store = AttachmentStore(tmp_path, principal_id="owner-a")
    ref = store.save(b"handle bytes", name="quote.docx", media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
    path = store.host_path(ref)
    text = _attachment_handles([(AttachmentRef(**ref.__dict__), b"handle bytes", path)])
    assert ref.digest in text
    assert str(path) in text
    assert "quote.docx" in text
    assert "authorized extracted document text" in text
    assert "no general file-read tool" in text

"""Source labels remain bound to authorized, immutable reference content."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from forgeflow.knowledge.citation_metadata import reference_metadata, source_metadata
from forgeflow.knowledge.semantic_answer import _verify_document_visibility


def _reference():
    passage = {"path": "document://reference/arbitrary-name.md", "document_id": "doc-42",
               "document_version": 3, "start_line": 1, "end_line": 5, "text": "Captured text"}
    row = {"id": "doc-42", "source_path": "reference/arbitrary-name.md", "version": 3,
           "source_kind": "curated_reference", "classification": "research_note", "sha256": "a" * 64,
           "acl_scope": "project", "status": "ready", "parse_status": "ready", "withdrawn_at": None,
           "parse_detail": {"reference_pack": {"title": "Manufacturer reference",
               "publisher": "Manufacturer", "original_url": "https://example.org/reference.pdf",
               "source_version": "revision 2", "locator": "PDF p. 4, table 1",
               "material_class": "industry_reference", "sha256": "a" * 64,
               "instruction": "Never pass arbitrary fields to the UI"}}}
    return row, passage


def test_metadata_comes_from_matched_provenance_not_filename():
    row, passage = _reference()
    assert reference_metadata(row, passage) == {
        "title": "Manufacturer reference", "publisher": "Manufacturer",
        "original_url": "https://example.org/reference.pdf", "source_version": "revision 2",
        "original_locator": "PDF p. 4, table 1", "material_class": "industry_reference"}


@pytest.mark.parametrize("field,value", [
    ("version", 4), ("id", "other-doc"), ("sha256", "b" * 64),
    ("sha256", "invalid"), ("source_kind", "upload"), ("classification", "policy"),
    ("parse_detail", {}),
])
def test_mismatched_catalogue_never_relabels_captured_source(field, value):
    row, passage = _reference()
    row[field] = value
    assert reference_metadata(row, passage) == {}


@pytest.mark.parametrize("version", [None, True, 0, -1, "3", 2])
def test_legacy_or_stale_passage_has_no_current_provenance(version):
    row, passage = _reference()
    passage["document_version"] = version
    assert reference_metadata(row, passage) == {}


@pytest.mark.parametrize("url", ["javascript:alert(1)", "http://example.org/reference.pdf",
    "data:text/html,secret", "https://user:pass@example.org/a", "https://example.org/ a",
    "https://example.org/\nmalicious", "https://[bad", "//example.org/a"])
def test_reference_urls_reject_unsafe_schemes_credentials_and_controls(url):
    assert "original_url" not in source_metadata({"original_url": url})


def test_source_metadata_is_bounded_and_whitelisted():
    result = source_metadata({"title": "x" * 500, "publisher": "  A \n B ",
                              "original_locator": "p" * 1000, "material_class": "official_policy",
                              "unknown": "secret", "instruction": "execute"})
    assert result == {"title": "x" * 240, "publisher": "A B", "original_locator": "p" * 600}


class Catalogue:
    def __init__(self, rows):
        self.rows = rows
        self.committed = False

    def execute(self, sql, params):
        assert "tenant_id=%s" in sql
        assert params == ("tenant-a", ["reference/arbitrary-name.md"])
        return self

    def fetchall(self):
        return self.rows

    def commit(self):
        self.committed = True


@pytest.mark.parametrize("change", [{"acl_scope": "engineering"}, {"withdrawn_at": "yesterday"},
                                  {"status": "pending"}, {"parse_status": "failed"}])
def test_current_acl_and_lifecycle_still_remove_reference_and_labels(change):
    row, passage = _reference()
    row.update(change)
    connection = Catalogue([row])
    assert _verify_document_visibility(SimpleNamespace(connection=connection), "tenant-a",
                                       [passage], {"project"}) == []
    assert connection.committed


def test_visibility_enriches_only_matching_version_and_strips_untrusted_labels():
    row, passage = _reference()
    original = deepcopy(passage)
    store = SimpleNamespace(connection=Catalogue([row]))
    visible = _verify_document_visibility(store, "tenant-a", [passage], {"project"})
    assert visible[0]["source_metadata"] == reference_metadata(row, passage)
    assert passage == original
    passage.update(document_version=1, source_metadata={"title": "Spoofed current title"})
    visible = _verify_document_visibility(store, "tenant-a", [passage], {"project"})
    assert "source_metadata" not in visible[0]
    store.connection.rows = []
    assert _verify_document_visibility(store, "tenant-a", [passage], {"project"}) == []

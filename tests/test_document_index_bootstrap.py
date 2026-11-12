from forgeflow.semantic import index


class Catalog:
    def __init__(self, published):
        self.value = published
    def published(self, tenant_id):
        return self.value


def test_first_document_bootstraps_generation_without_operator(monkeypatch):
    calls = []
    monkeypatch.setattr(index, "index_catalog_documents",
                        lambda *args, **kwargs: calls.append("bootstrap") or 1)
    monkeypatch.setattr(index, "index_single_document",
                        lambda *args, **kwargs: calls.append("single") or 1)
    index.ensure_document_indexed(object(), object(), "tenant", "doc.md", Catalog(None))
    assert calls == ["bootstrap"]


def test_existing_generation_uses_incremental_index(monkeypatch):
    calls = []
    monkeypatch.setattr(index, "index_catalog_documents",
                        lambda *args, **kwargs: calls.append("bootstrap") or 1)
    monkeypatch.setattr(index, "index_single_document",
                        lambda *args, **kwargs: calls.append("single") or 1)
    index.ensure_document_indexed(object(), object(), "tenant", "doc.md", Catalog("gen"))
    assert calls == ["single"]

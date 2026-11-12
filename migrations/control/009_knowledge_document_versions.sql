CREATE TABLE IF NOT EXISTS knowledge_document_versions (
    id UUID PRIMARY KEY,
    document_id UUID NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    tenant_id TEXT NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    version INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    content TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (document_id, version)
);
CREATE INDEX IF NOT EXISTS knowledge_document_versions_tenant_path
    ON knowledge_document_versions (tenant_id, source_path, version DESC);

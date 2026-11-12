CREATE TABLE IF NOT EXISTS knowledge_documents (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    source_path TEXT NOT NULL,
    filename TEXT NOT NULL,
    kind TEXT NOT NULL,
    acl_scope TEXT NOT NULL DEFAULT 'project',
    sha256 TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    content TEXT NOT NULL,
    size_bytes BIGINT NOT NULL,
    status TEXT NOT NULL DEFAULT 'ready',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_documents_tenant_path
    ON knowledge_documents (tenant_id, source_path);
CREATE INDEX IF NOT EXISTS knowledge_documents_tenant_status
    ON knowledge_documents (tenant_id, status);

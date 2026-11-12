-- Persisted Document Mind images.  The vendor's signed URL is downloaded within
-- its validity window; only the content hash, type, size, owning document and
-- ACL scope are stored here.  No vendor URL or STS token is persisted.
CREATE TABLE IF NOT EXISTS knowledge_document_images (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    sha256 text NOT NULL,
    document_id text NOT NULL,
    document_version integer NOT NULL DEFAULT 1,
    source_sha256 text NOT NULL,
    acl_scope text NOT NULL DEFAULT 'project',
    content_type text NOT NULL,
    byte_size integer NOT NULL CHECK (byte_size > 0),
    storage_path text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, sha256)
);
CREATE INDEX IF NOT EXISTS knowledge_document_images_document
    ON knowledge_document_images (tenant_id, document_id, document_version);

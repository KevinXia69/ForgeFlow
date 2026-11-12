-- One binary may be referenced by several documents with different ACL scopes.
-- Keep the file content-addressed once per tenant, but store one link row per
-- (tenant, content, document, version) so an engineering document cannot widen
-- or narrow a project document's access.
ALTER TABLE knowledge_document_images
    DROP CONSTRAINT IF EXISTS knowledge_document_images_pkey;
ALTER TABLE knowledge_document_images
    ADD PRIMARY KEY (tenant_id, sha256, document_id, document_version);

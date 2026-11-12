INSERT INTO knowledge_document_versions(
    id, document_id, tenant_id, source_path, version, sha256, content, size_bytes
)
SELECT gen_random_uuid(), d.id, d.tenant_id, d.source_path, d.version,
       d.sha256, d.content, d.size_bytes
FROM knowledge_documents d
WHERE NOT EXISTS (
    SELECT 1 FROM knowledge_document_versions v
    WHERE v.document_id = d.id AND v.version = d.version
);

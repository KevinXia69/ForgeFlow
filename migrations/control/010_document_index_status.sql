ALTER TABLE knowledge_documents
    ADD COLUMN IF NOT EXISTS indexed_generation TEXT,
    ADD COLUMN IF NOT EXISTS indexed_at TIMESTAMPTZ;

-- 014: thread identity, knowledge structure/permissions and outbox cursors.
--
-- W4 needs one thread per business object so a new version of the same object
-- merges instead of opening a duplicate item.  W5 needs citable document blocks,
-- explicit parse completeness, per-role ACLs and serialised publication.  W6
-- needs a durable cursor over the Odoo transactional outbox.

-- --- threads ---------------------------------------------------------------
-- Collapse any duplicate object threads before enforcing uniqueness: keep the
-- oldest thread for each object and re-point the tickets that used the others.
WITH ranked AS (
    SELECT id, tenant_id, object_ref,
           row_number() OVER (PARTITION BY tenant_id, object_ref ORDER BY created_at, id) AS rn
    FROM control_threads
)
UPDATE control_tickets t
SET thread_id = ranked.id
FROM ranked
JOIN control_threads original ON original.tenant_id = ranked.tenant_id
WHERE ranked.rn = 1
  AND original.id = t.thread_id
  AND t.thread_id <> ranked.id;

WITH ranked AS (
    SELECT id, tenant_id, object_ref,
           row_number() OVER (PARTITION BY tenant_id, object_ref ORDER BY created_at, id) AS rn
    FROM control_threads
)
DELETE FROM control_threads
WHERE id IN (SELECT id FROM ranked WHERE rn > 1);

CREATE UNIQUE INDEX IF NOT EXISTS control_threads_tenant_object
    ON control_threads (tenant_id, object_ref);
CREATE INDEX IF NOT EXISTS control_threads_tenant
    ON control_threads (tenant_id);

-- --- knowledge documents ---------------------------------------------------
ALTER TABLE knowledge_documents
    ADD COLUMN IF NOT EXISTS parse_status text NOT NULL DEFAULT 'complete',
    ADD COLUMN IF NOT EXISTS parse_detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS unparsed_scope text[] NOT NULL DEFAULT '{}'::text[],
    ADD COLUMN IF NOT EXISTS review_state text NOT NULL DEFAULT 'not_required',
    ADD COLUMN IF NOT EXISTS source_kind text NOT NULL DEFAULT 'upload',
    ADD COLUMN IF NOT EXISTS classification text NOT NULL DEFAULT 'enterprise_document',
    ADD COLUMN IF NOT EXISTS withdrawn_at timestamptz,
    ADD COLUMN IF NOT EXISTS published_generation text,
    ADD COLUMN IF NOT EXISTS content_hash text;

ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_parse_status_check;
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_documents_parse_status_check
    CHECK (parse_status IN ('complete', 'partial', 'failed', 'needs_review'));

ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_review_state_check;
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_documents_review_state_check
    CHECK (review_state IN ('not_required', 'required', 'confirmed', 'rejected'));

ALTER TABLE knowledge_documents DROP CONSTRAINT IF EXISTS knowledge_documents_classification_check;
ALTER TABLE knowledge_documents ADD CONSTRAINT knowledge_documents_classification_check
    CHECK (classification IN ('enterprise_document', 'contract', 'sop', 'research_note',
                             'project_plan', 'verified_capability'));

UPDATE knowledge_documents
SET content_hash = encode(sha256(convert_to(content, 'UTF8')), 'hex')
WHERE content_hash IS NULL;

CREATE INDEX IF NOT EXISTS knowledge_documents_withdrawn
    ON knowledge_documents (tenant_id, withdrawn_at);

-- Citable document blocks.  A citation points at a block identity (page, table
-- cell, sheet range, paragraph or line span), never at a character offset in a
-- flattened string.
CREATE TABLE IF NOT EXISTS knowledge_document_blocks (
    id uuid PRIMARY KEY,
    document_id uuid NOT NULL REFERENCES knowledge_documents(id) ON DELETE CASCADE,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    version integer NOT NULL,
    ord integer NOT NULL,
    block_type text NOT NULL
        CHECK (block_type IN ('section', 'paragraph', 'table', 'row', 'cell',
                              'heading', 'line', 'page')),
    parent_id uuid,
    section_path text,
    page integer,
    paragraph_index integer,
    table_index integer,
    row_index integer,
    cell_ref text,
    sheet text,
    cell_range text,
    line_start integer,
    line_end integer,
    text text NOT NULL,
    content_hash text NOT NULL,
    locator text NOT NULL,
    UNIQUE (document_id, version, ord)
);

CREATE INDEX IF NOT EXISTS knowledge_document_blocks_document
    ON knowledge_document_blocks (tenant_id, document_id, version, ord);
CREATE INDEX IF NOT EXISTS knowledge_document_blocks_locator
    ON knowledge_document_blocks (tenant_id, locator);

-- Per-role ACL used *before* recall.  An absent row means "nothing readable",
-- never "unrestricted": the resolver returns an explicit empty set.
CREATE TABLE IF NOT EXISTS knowledge_role_scopes (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('admin', 'planner', 'approver', 'observer')),
    acl_scope text NOT NULL,
    PRIMARY KEY (tenant_id, role, acl_scope)
);

-- Default enterprise policy: every role reads project knowledge, only admins
-- read engineering material.  A tenant may narrow this, and narrowing must take
-- effect on the next query.
INSERT INTO knowledge_role_scopes(tenant_id, role, acl_scope)
SELECT t.id,
       CASE WHEN scope_value = 'engineering' THEN 'admin' ELSE role_value END,
       scope_value
FROM control_tenants t
CROSS JOIN (VALUES ('admin'), ('planner'), ('approver'), ('observer')) AS roles(role_value)
CROSS JOIN (VALUES ('project'), ('engineering')) AS scopes(scope_value)
WHERE (scope_value = 'project' OR role_value = 'admin')
ON CONFLICT DO NOTHING;

-- Serialised publication.  A per-tenant counter row is the lock and the
-- monotonic sequence; two concurrent publishers cannot both win.
CREATE TABLE IF NOT EXISTS knowledge_publication_counters (
    tenant_id text PRIMARY KEY REFERENCES control_tenants(id) ON DELETE CASCADE,
    last_sequence bigint NOT NULL DEFAULT 0
);

ALTER TABLE knowledge_generations
    ADD COLUMN IF NOT EXISTS publish_sequence bigint,
    ADD COLUMN IF NOT EXISTS published_by text,
    ADD COLUMN IF NOT EXISTS document_count integer,
    ADD COLUMN IF NOT EXISTS point_count integer;

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_generations_sequence
    ON knowledge_generations (tenant_id, publish_sequence)
    WHERE publish_sequence IS NOT NULL;

-- --- Odoo transactional outbox cursor -------------------------------------
CREATE TABLE IF NOT EXISTS control_outbox_cursors (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    source_key text NOT NULL,
    last_outbox_id bigint NOT NULL DEFAULT 0,
    last_consumed_at timestamptz,
    PRIMARY KEY (tenant_id, source_key)
);

-- --- attention routing -----------------------------------------------------
CREATE INDEX IF NOT EXISTS control_attentions_open
    ON control_attentions (tenant_id, state, created_at);

-- --- plan linkage ----------------------------------------------------------
ALTER TABLE control_odoo_plan_links
    ADD COLUMN IF NOT EXISTS plan_version integer,
    ADD COLUMN IF NOT EXISTS plan_hash text,
    ADD COLUMN IF NOT EXISTS environment_generation text,
    ADD COLUMN IF NOT EXISTS execution_state text;

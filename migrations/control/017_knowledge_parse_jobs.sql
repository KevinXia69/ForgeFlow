-- Document Mind parse jobs.  A job is durable so an API or worker restart can
-- resume it, and the partial unique index prevents paying twice for the same
-- tenant's identical source content while a job is still active.
CREATE TABLE IF NOT EXISTS knowledge_parse_jobs (
    id text PRIMARY KEY,
    tenant_id text NOT NULL,
    document_id text NOT NULL,
    document_version integer NOT NULL DEFAULT 1,
    source_path text NOT NULL,
    source_sha256 text NOT NULL,
    conversion_json text,
    vendor_job_id text,
    state text NOT NULL DEFAULT 'queued',
    attempts integer NOT NULL DEFAULT 0,
    next_poll_at timestamptz,
    error_code text,
    result_json text,
    blocks_json text,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    submitted_at timestamptz,
    completed_at timestamptz
);
CREATE UNIQUE INDEX IF NOT EXISTS knowledge_parse_jobs_active
    ON knowledge_parse_jobs (tenant_id, source_sha256)
    WHERE state IN ('queued', 'processing');
CREATE INDEX IF NOT EXISTS knowledge_parse_jobs_due
    ON knowledge_parse_jobs (state, next_poll_at);

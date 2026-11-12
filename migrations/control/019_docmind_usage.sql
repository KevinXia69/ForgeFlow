-- Per-tenant, per-day Document Mind usage and cost.  The vendor bills per
-- parsed page and per submitted job; keeping counters lets an operator see the
-- spend without exposing vendor credentials or per-document content.
CREATE TABLE IF NOT EXISTS knowledge_docmind_usage (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    window_start date NOT NULL,
    vendor_calls integer NOT NULL DEFAULT 0 CHECK (vendor_calls >= 0),
    pages integer NOT NULL DEFAULT 0 CHECK (pages >= 0),
    blocks integer NOT NULL DEFAULT 0 CHECK (blocks >= 0),
    tokens integer NOT NULL DEFAULT 0 CHECK (tokens >= 0),
    cost_micros bigint NOT NULL DEFAULT 0 CHECK (cost_micros >= 0),
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, window_start)
);
CREATE INDEX IF NOT EXISTS knowledge_docmind_usage_day
    ON knowledge_docmind_usage (window_start);

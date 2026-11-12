CREATE TABLE IF NOT EXISTS knowledge_generations (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    generation uuid NOT NULL,
    state text NOT NULL DEFAULT 'staging' CHECK (state IN ('staging', 'published', 'retired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    published_at timestamptz,
    PRIMARY KEY (tenant_id, generation)
);

CREATE UNIQUE INDEX IF NOT EXISTS knowledge_one_published_generation
    ON knowledge_generations (tenant_id) WHERE state = 'published';

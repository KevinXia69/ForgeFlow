CREATE TABLE IF NOT EXISTS control_source_subscriptions (
    id UUID PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    source_type TEXT NOT NULL,
    enabled BOOLEAN NOT NULL DEFAULT TRUE,
    interval_seconds INTEGER NOT NULL DEFAULT 60 CHECK (interval_seconds BETWEEN 5 AND 86400),
    config JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_scan_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE UNIQUE INDEX IF NOT EXISTS control_source_subscriptions_tenant_source
    ON control_source_subscriptions (tenant_id, source_type);

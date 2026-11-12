CREATE TABLE IF NOT EXISTS control_service_heartbeats (
    service_id TEXT PRIMARY KEY,
    tenant_id TEXT REFERENCES control_tenants(id) ON DELETE CASCADE,
    kind TEXT NOT NULL,
    last_seen TIMESTAMPTZ NOT NULL DEFAULT now(),
    detail JSONB NOT NULL DEFAULT '{}'::jsonb
);

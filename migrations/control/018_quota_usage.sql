-- Daily global/tenant usage counters.  A row with tenant_id='*' is the global
-- budget; limits themselves come from deployment configuration, not the DB.
CREATE TABLE IF NOT EXISTS control_quota_usage (
    tenant_id text NOT NULL,
    kind text NOT NULL,
    window_start date NOT NULL,
    used bigint NOT NULL DEFAULT 0,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, kind, window_start)
);

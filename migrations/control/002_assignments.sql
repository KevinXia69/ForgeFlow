CREATE TABLE IF NOT EXISTS control_assignments (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    runner text NOT NULL,
    session_id text,
    attempt integer NOT NULL DEFAULT 1,
    lease_owner text,
    lease_expires_at timestamptz,
    fencing_token uuid,
    state text NOT NULL DEFAULT 'leased' CHECK (state IN ('leased', 'running', 'completed', 'failed', 'expired')),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS control_assignments_ticket_idx
    ON control_assignments (tenant_id, ticket_id, attempt);

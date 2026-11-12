CREATE TABLE IF NOT EXISTS control_schema_migrations (
    version integer PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tenants (
    id text PRIMARY KEY,
    display_name text NOT NULL,
    policy_version integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_principals (
    id text PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    display_name text NOT NULL,
    role text NOT NULL CHECK (role IN ('admin', 'planner', 'approver', 'observer')),
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, id)
);

CREATE TABLE IF NOT EXISTS control_triggers (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    source_id text NOT NULL,
    external_event_id text NOT NULL,
    payload_hash text NOT NULL,
    occurred_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, source_id, external_event_id)
);

CREATE TABLE IF NOT EXISTS control_threads (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    object_ref text NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_tickets (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    thread_id uuid NOT NULL REFERENCES control_threads(id) ON DELETE CASCADE,
    case_ref uuid,
    goal text NOT NULL,
    state text NOT NULL DEFAULT 'queued',
    version integer NOT NULL DEFAULT 1,
    policy_version integer NOT NULL,
    mandate_id uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_trigger_admissions (
    trigger_id uuid PRIMARY KEY REFERENCES control_triggers(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL UNIQUE REFERENCES control_tickets(id) ON DELETE CASCADE,
    admitted_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_events (
    id bigserial PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    sequence integer NOT NULL,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, ticket_id, sequence)
);

CREATE TABLE IF NOT EXISTS control_effects (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    kind text NOT NULL,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    state text NOT NULL DEFAULT 'pending' CHECK (state IN ('pending', 'running', 'completed', 'failed')),
    available_at timestamptz NOT NULL DEFAULT now(),
    lease_owner text,
    lease_expires_at timestamptz,
    attempt integer NOT NULL DEFAULT 0,
    fencing_token uuid,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS control_effects_claim_idx
    ON control_effects (state, available_at, created_at);

CREATE TABLE IF NOT EXISTS control_inputs (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    trigger_id uuid REFERENCES control_triggers(id) ON DELETE SET NULL,
    trust_level text NOT NULL DEFAULT 'untrusted',
    body jsonb NOT NULL DEFAULT '{}'::jsonb,
    consumed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS control_attentions (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    need_type text NOT NULL CHECK (need_type IN ('clarification', 'fact', 'approval', 'failure')),
    state text NOT NULL DEFAULT 'open' CHECK (state IN ('open', 'answered', 'dismissed')),
    question text NOT NULL,
    answer jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now()
);

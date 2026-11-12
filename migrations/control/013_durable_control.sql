-- 013: durable task control.
--
-- Adds the domain objects the control engine needs to be the single writer of
-- task state: mandates, inputs, dependencies, subscriptions, assignment
-- results, effect side-effect classes, ticket deadlines/budgets and an atomic
-- per-ticket event sequence allocator.

-- Atomic per-ticket event sequence allocation ---------------------------------
-- Replaces the MAX(sequence)+1 read-modify-write that two concurrent writers
-- can race on.  A row is created on first allocation and bumped in place.
CREATE TABLE IF NOT EXISTS control_ticket_sequences (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    last_sequence bigint NOT NULL DEFAULT 0,
    PRIMARY KEY (tenant_id, ticket_id)
);

INSERT INTO control_ticket_sequences(tenant_id, ticket_id, last_sequence)
SELECT tenant_id, ticket_id, COALESCE(MAX(sequence), 0)
FROM control_events
GROUP BY tenant_id, ticket_id
ON CONFLICT (tenant_id, ticket_id) DO NOTHING;

-- Mandates: scoped authority to investigate/prepare, never to write business data.
CREATE TABLE IF NOT EXISTS control_mandates (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    principal_id text NOT NULL,
    policy_version integer NOT NULL,
    capabilities text[] NOT NULL DEFAULT '{}'::text[],
    tools text[] NOT NULL DEFAULT '{}'::text[],
    knowledge_scopes text[] NOT NULL DEFAULT '{}'::text[],
    scope_mode text NOT NULL DEFAULT 'objects'
        CHECK (scope_mode IN ('all', 'objects', 'none')),
    object_refs text[] NOT NULL DEFAULT '{}'::text[],
    budget_tokens bigint,
    budget_effects integer,
    deadline_at timestamptz,
    state text NOT NULL DEFAULT 'active'
        CHECK (state IN ('active', 'revoked', 'expired')),
    revision integer NOT NULL DEFAULT 1,
    issued_at timestamptz NOT NULL DEFAULT now(),
    revoked_at timestamptz,
    revoke_reason text,
    source_ticket_id uuid,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS control_mandates_tenant_state
    ON control_mandates (tenant_id, state);
CREATE INDEX IF NOT EXISTS control_mandates_object_refs
    ON control_mandates USING gin (object_refs);

-- Tickets: deadline, budget accounting, progress and canonical lifecycle fields.
ALTER TABLE control_tickets
    ADD COLUMN IF NOT EXISTS goal_kind text,
    ADD COLUMN IF NOT EXISTS deadline_at timestamptz,
    ADD COLUMN IF NOT EXISTS budget_tokens bigint,
    ADD COLUMN IF NOT EXISTS budget_effects integer,
    ADD COLUMN IF NOT EXISTS spent_tokens bigint NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS spent_effects integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS progress_at timestamptz NOT NULL DEFAULT now(),
    ADD COLUMN IF NOT EXISTS progress_kind text,
    ADD COLUMN IF NOT EXISTS stop_reason text,
    ADD COLUMN IF NOT EXISTS closed_at timestamptz,
    ADD COLUMN IF NOT EXISTS created_by text;

UPDATE control_tickets SET goal_kind = left(regexp_replace(lower(goal), '\s+', ' ', 'g'), 120)
WHERE goal_kind IS NULL;

-- Assignments: one row per worker execution attempt, with the runner's
-- structured report and the fencing token it was issued under.
ALTER TABLE control_assignments
    ADD COLUMN IF NOT EXISTS capability text,
    ADD COLUMN IF NOT EXISTS mandate_id uuid REFERENCES control_mandates(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS result_json jsonb,
    ADD COLUMN IF NOT EXISTS error_code text,
    ADD COLUMN IF NOT EXISTS rejected_reason text,
    ADD COLUMN IF NOT EXISTS ceased_at timestamptz;

ALTER TABLE control_assignments DROP CONSTRAINT IF EXISTS control_assignments_state_check;
ALTER TABLE control_assignments ADD CONSTRAINT control_assignments_state_check
    CHECK (state IN ('leased', 'running', 'completed', 'failed', 'expired', 'cancelled', 'rejected'));

CREATE UNIQUE INDEX IF NOT EXISTS control_assignments_active_ticket
    ON control_assignments (tenant_id, ticket_id)
    WHERE state IN ('leased', 'running');

-- Effects: side-effect class drives recovery policy, and business writes never
-- get blindly replayed.
ALTER TABLE control_effects
    ADD COLUMN IF NOT EXISTS side_effect_class text NOT NULL DEFAULT 'read_only',
    ADD COLUMN IF NOT EXISTS total_deadline_at timestamptz,
    ADD COLUMN IF NOT EXISTS idempotency_key text,
    ADD COLUMN IF NOT EXISTS mandate_id uuid REFERENCES control_mandates(id) ON DELETE SET NULL,
    ADD COLUMN IF NOT EXISTS reconcile_state text,
    ADD COLUMN IF NOT EXISTS reconcile_detail jsonb;

ALTER TABLE control_effects DROP CONSTRAINT IF EXISTS control_effects_side_effect_class_check;
ALTER TABLE control_effects ADD CONSTRAINT control_effects_side_effect_class_check
    CHECK (side_effect_class IN ('read_only', 'idempotent_metadata', 'model_inference',
                                 'business_write', 'notification'));

ALTER TABLE control_effects DROP CONSTRAINT IF EXISTS control_effects_state_check;
ALTER TABLE control_effects ADD CONSTRAINT control_effects_state_check
    CHECK (state IN ('pending', 'running', 'completed', 'failed', 'cancelled',
                     'reconcile_required'));

ALTER TABLE control_effects DROP CONSTRAINT IF EXISTS control_effects_reconcile_state_check;
ALTER TABLE control_effects ADD CONSTRAINT control_effects_reconcile_state_check
    CHECK (reconcile_state IS NULL
           OR reconcile_state IN ('unknown', 'confirmed_applied', 'confirmed_not_applied'));

CREATE INDEX IF NOT EXISTS control_effects_ticket_idx
    ON control_effects (tenant_id, ticket_id, state);

-- Inputs: durable delivery of user messages, source events and answers into a
-- running ticket.  An input is consumed exactly once per assignment.
ALTER TABLE control_inputs
    ADD COLUMN IF NOT EXISTS kind text NOT NULL DEFAULT 'event',
    ADD COLUMN IF NOT EXISTS dedupe_key text,
    ADD COLUMN IF NOT EXISTS state text NOT NULL DEFAULT 'pending',
    ADD COLUMN IF NOT EXISTS payload_hash text,
    ADD COLUMN IF NOT EXISTS consumed_by uuid;

ALTER TABLE control_inputs DROP CONSTRAINT IF EXISTS control_inputs_state_check;
ALTER TABLE control_inputs ADD CONSTRAINT control_inputs_state_check
    CHECK (state IN ('pending', 'delivered', 'consumed', 'rejected'));

CREATE UNIQUE INDEX IF NOT EXISTS control_inputs_dedupe
    ON control_inputs (tenant_id, ticket_id, dedupe_key)
    WHERE dedupe_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS control_inputs_pending
    ON control_inputs (tenant_id, ticket_id, state);

-- Task dependency graph: a worker may propose subtasks but the server bounds
-- depth, breadth and budget.
CREATE TABLE IF NOT EXISTS control_task_dependencies (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    depends_on_ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    state text NOT NULL DEFAULT 'pending'
        CHECK (state IN ('pending', 'satisfied', 'failed', 'cancelled')),
    reason text,
    created_at timestamptz NOT NULL DEFAULT now(),
    satisfied_at timestamptz,
    UNIQUE (tenant_id, ticket_id, depends_on_ticket_id)
);

CREATE INDEX IF NOT EXISTS control_task_dependencies_parent
    ON control_task_dependencies (tenant_id, depends_on_ticket_id, state);

-- Subscriptions: a business object stays attached to an ongoing ticket so a new
-- version of the same object updates the existing work instead of opening
-- another one.
CREATE TABLE IF NOT EXISTS control_subscriptions (
    id uuid PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    object_ref text NOT NULL,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    deactivated_at timestamptz,
    UNIQUE (tenant_id, object_ref, ticket_id)
);

CREATE INDEX IF NOT EXISTS control_subscriptions_lookup
    ON control_subscriptions (tenant_id, object_ref) WHERE active;

-- Persistent source cursors and condition state.  Polling a business object
-- must not depend on process memory.
CREATE TABLE IF NOT EXISTS control_source_state (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    source_id text NOT NULL,
    state jsonb NOT NULL DEFAULT '{}'::jsonb,
    cursor_value bigint NOT NULL DEFAULT 0,
    last_event_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, source_id)
);

-- Paged/incremental scans remember how far they have consumed a result set.
CREATE TABLE IF NOT EXISTS control_source_cursors (
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    cursor_key text NOT NULL,
    cursor_value text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (tenant_id, cursor_key)
);

-- Attention: extend the fixed need vocabulary.  A successful worker result is
-- not a reason to ask a human for anything.
ALTER TABLE control_attentions DROP CONSTRAINT IF EXISTS control_attentions_need_type_check;
ALTER TABLE control_attentions ADD CONSTRAINT control_attentions_need_type_check
    CHECK (need_type IN ('clarification', 'fact', 'approval', 'failure', 'rule',
                         'budget', 'result_unknown'));

ALTER TABLE control_attentions
    ADD COLUMN IF NOT EXISTS blocking boolean NOT NULL DEFAULT true,
    ADD COLUMN IF NOT EXISTS assignee_principal_id text,
    ADD COLUMN IF NOT EXISTS detail jsonb;

CREATE TABLE IF NOT EXISTS control_odoo_plan_links (
    plan_id integer PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    ticket_id uuid NOT NULL REFERENCES control_tickets(id) ON DELETE CASCADE,
    thread_id uuid NOT NULL REFERENCES control_threads(id) ON DELETE CASCADE,
    attention_id uuid NOT NULL REFERENCES control_attentions(id) ON DELETE CASCADE,
    principal_id text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    UNIQUE (tenant_id, plan_id)
);

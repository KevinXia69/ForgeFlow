CREATE TABLE IF NOT EXISTS control_odoo_bindings (
    principal_id text PRIMARY KEY,
    tenant_id text NOT NULL REFERENCES control_tenants(id) ON DELETE CASCADE,
    odoo_user_id integer NOT NULL,
    role text NOT NULL CHECK (role IN ('planner', 'approver', 'admin', 'observer')),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS control_odoo_bindings_tenant_idx
    ON control_odoo_bindings (tenant_id, odoo_user_id);

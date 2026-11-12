-- 016: plan links are tenant-scoped, not global.
--
-- ``control_odoo_plan_links.plan_id`` was a global primary key.  Odoo plan ids
-- are per ERP instance, so two tenants commonly have a plan 1; with a global key
-- the second tenant's insert hit the first tenant's row through
-- ``ON CONFLICT (plan_id) DO UPDATE`` and silently rewrote its task link.
--
-- Replace the key with the composite identity that actually exists.

ALTER TABLE control_odoo_plan_links
    DROP CONSTRAINT IF EXISTS control_odoo_plan_links_pkey;

ALTER TABLE control_odoo_plan_links
    ADD CONSTRAINT control_odoo_plan_links_pkey PRIMARY KEY (tenant_id, plan_id);

-- The table already carries a UNIQUE (tenant_id, plan_id); drop the duplicate so
-- PostgreSQL does not maintain two identical indexes.
ALTER TABLE control_odoo_plan_links
    DROP CONSTRAINT IF EXISTS control_odoo_plan_links_tenant_id_plan_id_key;

CREATE INDEX IF NOT EXISTS control_odoo_plan_links_plan
    ON control_odoo_plan_links (plan_id);

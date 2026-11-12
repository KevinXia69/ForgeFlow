-- Principal ids are tenant-scoped identities, not global accounts.  The original
-- id-only primary key let two tenants share one row, so seeding the second
-- tenant silently rebound the first tenant's principal.  The composite key makes
-- the same id legal in two tenants and keeps every membership row explicit.
ALTER TABLE control_principals DROP CONSTRAINT IF EXISTS control_principals_pkey;
ALTER TABLE control_principals ADD PRIMARY KEY (tenant_id, id);

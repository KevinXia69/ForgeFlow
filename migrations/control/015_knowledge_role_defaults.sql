-- 015: every tenant starts with an explicit ACL policy.
--
-- An empty ACL set means "read nothing", so a tenant created after the initial
-- migration must still receive its default policy.  A trigger keeps that true for
-- every creation path (seed_tenant, onboarding, tests) instead of relying on one
-- caller remembering to seed.

CREATE OR REPLACE FUNCTION knowledge_seed_role_scopes(target_tenant text)
RETURNS void LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO knowledge_role_scopes(tenant_id, role, acl_scope)
    SELECT target_tenant, roles.role_value, scopes.scope_value
    FROM (VALUES ('admin'), ('planner'), ('approver'), ('observer')) AS roles(role_value)
    CROSS JOIN (VALUES ('project'), ('engineering')) AS scopes(scope_value)
    WHERE scopes.scope_value = 'project' OR roles.role_value = 'admin'
    ON CONFLICT DO NOTHING;
END;
$$;

CREATE OR REPLACE FUNCTION knowledge_seed_role_scopes_trigger()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    PERFORM knowledge_seed_role_scopes(NEW.id);
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS control_tenants_seed_knowledge_scopes ON control_tenants;
CREATE TRIGGER control_tenants_seed_knowledge_scopes
    AFTER INSERT ON control_tenants
    FOR EACH ROW EXECUTE FUNCTION knowledge_seed_role_scopes_trigger();

-- Backfill tenants created before the trigger existed.
SELECT knowledge_seed_role_scopes(id) FROM control_tenants;

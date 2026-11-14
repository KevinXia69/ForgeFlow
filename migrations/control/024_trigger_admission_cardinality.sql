-- A trigger is admitted once; several triggers may attach to the same ticket.
-- 013 introduced merging but retained 001's UNIQUE(ticket_id), so the engine's
-- ON CONFLICT DO NOTHING could omit a merged trigger's admission. Recover only
-- the exact association already witnessed by its input AND input_attached event.
-- Run through ControlStore.migrate (one transaction). Never replay a command.
SET LOCAL lock_timeout = '10s';
SET LOCAL statement_timeout = '60s';
LOCK TABLE control_triggers, control_trigger_admissions, control_inputs,
    control_events, control_tickets, control_threads IN SHARE ROW EXCLUSIVE MODE;

CREATE TEMPORARY TABLE admission_repair_024 ON COMMIT DROP AS
SELECT t.id AS trigger_id, t.tenant_id, i.ticket_id, i.id AS input_id,
       e.id AS event_id, e.created_at AS original_admitted_at
FROM control_triggers t
JOIN control_inputs i ON i.trigger_id = t.id
JOIN control_tickets tk ON tk.id = i.ticket_id
JOIN control_threads th ON th.id = tk.thread_id
JOIN control_events e ON e.kind = 'input_attached'
    AND e.payload->>'trigger_id' = t.id::text
    AND e.payload->>'input_id' = i.id::text
WHERE NOT EXISTS (
    SELECT 1 FROM control_trigger_admissions a WHERE a.trigger_id = t.id
)
AND i.tenant_id = t.tenant_id AND tk.tenant_id = t.tenant_id
AND th.tenant_id = t.tenant_id AND e.tenant_id = t.tenant_id
AND e.ticket_id = i.ticket_id
AND i.kind = 'event' AND i.dedupe_key = 'trigger:' || t.id::text
AND i.payload_hash = t.payload_hash AND i.body->>'payload_hash' = t.payload_hash
AND i.body->>'source_id' = t.source_id
AND i.body->>'external_event_id' = t.external_event_id
AND i.body->>'object_ref' = th.object_ref
AND e.payload->>'source_id' = t.source_id
AND e.payload->>'object_ref' = th.object_ref
-- Count all references, including contradictory/cross-tenant evidence. Filtering
-- down to one valid row would otherwise conceal a second conflicting witness.
AND (SELECT count(*) FROM control_inputs other WHERE other.trigger_id = t.id) = 1
AND (SELECT count(*) FROM control_events other WHERE other.kind = 'input_attached'
     AND (other.payload->>'trigger_id' = t.id::text
          OR other.payload->>'input_id' = i.id::text)) = 1;

DO $$
DECLARE
    orphan_count bigint;
    proven_count bigint;
    ticket_attribute smallint;
    unique_name text;
BEGIN
    SELECT count(*) INTO orphan_count FROM control_triggers t
    WHERE NOT EXISTS (
        SELECT 1 FROM control_trigger_admissions a WHERE a.trigger_id = t.id
    );
    SELECT count(DISTINCT trigger_id) INTO proven_count FROM admission_repair_024;
    IF proven_count <> orphan_count
       OR proven_count <> (SELECT count(*) FROM admission_repair_024) THEN
        RAISE EXCEPTION 'admission_repair_024_unproven_or_ambiguous_orphan';
    END IF;

    SELECT attnum INTO STRICT ticket_attribute FROM pg_attribute
    WHERE attrelid = 'control_trigger_admissions'::regclass AND attname = 'ticket_id';
    -- Fail on an unexpected schema instead of silently accepting a partial fix.
    SELECT conname INTO STRICT unique_name FROM pg_constraint
    WHERE conrelid = 'control_trigger_admissions'::regclass AND contype = 'u'
      AND conkey = ARRAY[ticket_attribute];
    EXECUTE format('ALTER TABLE control_trigger_admissions DROP CONSTRAINT %I', unique_name);
    IF EXISTS (
        SELECT 1 FROM pg_index WHERE indrelid = 'control_trigger_admissions'::regclass
        AND indisunique AND indnkeyatts = 1 AND indkey[0] = ticket_attribute
    ) THEN
        RAISE EXCEPTION 'admission_repair_024_unexpected_ticket_unique_index';
    END IF;
END $$;

CREATE INDEX control_trigger_admissions_ticket_idx ON control_trigger_admissions (ticket_id);

-- Operational repair evidence, not a new business event. Intentionally no FKs:
-- historical IDs must not interfere with normal tenant/ticket cascade deletion.
CREATE TABLE control_trigger_admission_repairs (
    trigger_id uuid PRIMARY KEY,
    tenant_id text NOT NULL,
    ticket_id uuid NOT NULL,
    input_id uuid NOT NULL,
    event_id bigint NOT NULL,
    original_admitted_at timestamptz NOT NULL,
    repaired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    migration_version integer NOT NULL DEFAULT 24 CHECK (migration_version = 24)
);

INSERT INTO control_trigger_admissions (trigger_id, ticket_id, admitted_at)
SELECT trigger_id, ticket_id, original_admitted_at FROM admission_repair_024;

INSERT INTO control_trigger_admission_repairs
    (trigger_id, tenant_id, ticket_id, input_id, event_id, original_admitted_at)
SELECT trigger_id, tenant_id, ticket_id, input_id, event_id, original_admitted_at
FROM admission_repair_024;

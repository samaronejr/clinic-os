"""Machine hold expiry joins the audit ledger (todo 22 gate r1, SC-4).

Every held -> expired edge appends the registered
``scheduling.appointment.expired`` event to the organization's hash chain in
the expiring transaction: the W job, an explicit ``expire`` and the lazy release
inside an overlapping booking alike. Like the other SQL-originated ledger
events, the appender is an owner-owned writer; it is a trigger so that no
expiry path can skip it. W has no human actor, so the tenant actor-scope check
admits a null actor for exactly this fixed metadata-only shape.

The W expiry also takes the appointment write gates (clinic, practitioner,
UUID-ordered resources, patient; ``apps.scheduling.locks``) before its row lock,
like every other appointment write. Its ledger append therefore never waits on
the organization's audit lock while holding a row that a gated writer, which may
already hold that lock, is about to lock.

Rollback is a restore once a machine expiry event exists.
"""

from typing import Final

from apps.scheduling.migrations._lifecycle_sql import _GUARD_SQL_PART_8

_NULL_ACTOR_SCOPE: Final = """
ALTER TABLE clinic_app.audit_event DROP CONSTRAINT audit_event_actor_scope_ck;
ALTER TABLE clinic_app.audit_event ADD CONSTRAINT audit_event_actor_scope_ck
    CHECK (
        organization_id = '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
        OR actor_user_id IS NOT NULL
        OR (
            event_type = 'scheduling.appointment.expired'
            AND component_id = 'clinic-os-worker'
            AND component_ip IS NULL
            AND affected_record_type = 'scheduling.appointment'
            AND payload ->> 'object_verb' = 'expired'
            AND payload - 'clinic_id' - 'object_verb' = '{}'::pg_catalog.jsonb
        )
    );
"""

_HUMAN_ACTOR_SCOPE: Final = """
ALTER TABLE clinic_app.audit_event DROP CONSTRAINT audit_event_actor_scope_ck;
ALTER TABLE clinic_app.audit_event ADD CONSTRAINT audit_event_actor_scope_ck
    CHECK (
        organization_id = '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
        OR actor_user_id IS NOT NULL
    );
"""

# The content hash is apps.audit.canonical._content_hash for this one closed
# shape: RFC 8785 key order without whitespace, behind the clinic-audit-v1 NUL
# domain prefix. Every value is a UUID, a fixed ASCII string or the UTC
# microsecond timestamp, so nothing needs escaping. The tip read, chaining and
# insert follow the ledger append under the organization's audit lock. Its key
# omits the other ledger writers' redundant COLLATE pg_catalog."C": a database
# default collation is deterministic, so hashtextextended hashes the same bytes
# (the scheduling clock census cannot bind collation names).
_APPEND: Final = """
CREATE FUNCTION clinic_app.audit_appointment_expired()
RETURNS trigger
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    occurred_at_utc pg_catalog.timestamptz := pg_catalog.clock_timestamp();
    payload pg_catalog.text := '{"clinic_id":"' || NEW.clinic_id::pg_catalog.text
        || '","object_verb":"expired"}';
    content_hash pg_catalog.bytea;
    previous_hash pg_catalog.bytea;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'audit append requires read committed isolation';
    END IF;
    content_hash := pg_catalog.sha256(
        pg_catalog.decode('636c696e69632d61756469742d763100', 'hex')
        || pg_catalog.convert_to(
            '{"actor":{"user_id":null},"affected_record_id":"'
            || NEW.id::pg_catalog.text
            || '","affected_record_type":"scheduling.appointment",'
            || '"component":{"identifier":"clinic-os-worker","ip":null},'
            || '"event_type":"scheduling.appointment.expired",'
            || '"occurred_at_utc":"'
            || pg_catalog.to_char(
                occurred_at_utc AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'
            )
            || '","organization_id":"' || NEW.organization_id::pg_catalog.text
            || '","payload":' || payload || ',"v":"clinic-audit-v1"}',
            'UTF8'
        )
    );
    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            'clinic-audit:' || NEW.organization_id::pg_catalog.text, 0::bigint
        )
    );
    SELECT audit_event.curr_hash
      INTO previous_hash
      FROM clinic_app.audit_event AS audit_event
     WHERE audit_event.organization_id = NEW.organization_id
     ORDER BY audit_event.seq DESC
     LIMIT 1;
    previous_hash := COALESCE(
        previous_hash,
        pg_catalog.decode(
            '0000000000000000000000000000000000000000000000000000000000000000',
            'hex'
        )
    );
    INSERT INTO clinic_app.audit_event (
        organization_id, actor_user_id, event_type, component_id, component_ip,
        affected_record_type, affected_record_id, occurred_at_utc, payload,
        prev_hash, curr_hash
    ) VALUES (
        NEW.organization_id, NULL, 'scheduling.appointment.expired',
        'clinic-os-worker', NULL, 'scheduling.appointment',
        NEW.id::pg_catalog.text, occurred_at_utc, payload::pg_catalog.jsonb,
        previous_hash, pg_catalog.sha256(content_hash || previous_hash)
    );
    RETURN NULL;
END
$function$;

ALTER FUNCTION clinic_app.audit_appointment_expired() OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.audit_appointment_expired()
    FROM PUBLIC, clinic_app, clinic_resolver;

CREATE TRIGGER scheduling_appointment_expiry_audit
    AFTER UPDATE OF status ON clinic_app.scheduling_appointment
    FOR EACH ROW
    WHEN (OLD.status = 'held' AND NEW.status = 'expired')
    EXECUTE FUNCTION clinic_app.audit_appointment_expired();
"""

# Migration 0007's live W expiry, replaced in place (owner, signature and grants
# are kept) and restored verbatim on reverse.
EXPIRE_HOLD: Final = (
    "CREATE OR REPLACE FUNCTION clinic_app.scheduling_expire_hold("
    + _GUARD_SQL_PART_8.split("CREATE FUNCTION clinic_app.scheduling_expire_hold(", 1)[
        1
    ].split("REVOKE ALL ON FUNCTION", 1)[0]
)
EXPIRE_HOLD_GATED: Final = EXPIRE_HOLD.replace(
    " receipt_revision integer;\nBEGIN\n",
    " receipt_revision integer;\n"
    " gate_organization uuid;\n"
    " gate_practitioner uuid;\n"
    " gate_patient uuid;\n"
    " gate_resource uuid;\n"
    "BEGIN\n",
    1,
).replace(
    " SELECT a.id, a.organization_id, a.revision INTO held_id, held_organization,\n",
    " -- Every appointment write takes these gates in this order before any row\n"
    " -- lock (apps.scheduling.locks); the expiry's ledger append then never\n"
    " -- waits for the audit lock while holding a row a gated writer will lock.\n"
    " SELECT a.organization_id, a.practitioner_id, a.patient_id\n"
    "  INTO gate_organization, gate_practitioner, gate_patient\n"
    "  FROM clinic_app.scheduling_appointment a\n"
    "  WHERE a.id=requested_appointment AND a.clinic_id=requested_clinic;\n"
    " IF gate_organization IS NOT NULL THEN\n"
    "  PERFORM pg_advisory_xact_lock(hashtextextended(\n"
    "   'clinic-lock-v1:clinic:' || requested_clinic::text, 0));\n"
    "  PERFORM pg_advisory_xact_lock(hashtextextended(\n"
    "   'clinic-lock-v1:user:' || gate_practitioner::text, 0));\n"
    "  FOR gate_resource IN SELECT DISTINCT r\n"
    "   FROM clinic_app.scheduling_appointment a\n"
    "   CROSS JOIN LATERAL unnest(a.resource_ids) AS r\n"
    "   WHERE a.id=requested_appointment AND a.clinic_id=requested_clinic\n"
    "   ORDER BY r LOOP\n"
    "   PERFORM pg_advisory_xact_lock(hashtextextended(\n"
    "    'clinic-lock-v1:resource:' || gate_resource::text, 0));\n"
    "  END LOOP;\n"
    "  PERFORM pg_advisory_xact_lock(hashtextextended('clinic-lock-v1:patient:'\n"
    "   || gate_organization::text || ':' || gate_patient::text, 0));\n"
    " END IF;\n"
    " SELECT a.id, a.organization_id, a.revision INTO held_id, held_organization,\n",
    1,
)

SQL: Final = (
    _NULL_ACTOR_SCOPE
    + _APPEND
    + "SET LOCAL ROLE clinic_resolver;\n"
    + EXPIRE_HOLD_GATED
    + "RESET ROLE;\n"
)

# Restoring the human-only scope would fail on any machine row, and ledger rows
# are never deleted: refuse first, with the restore requirement as the reason.
_ROLLBACK_GUARD: Final = """
DO $guard$
BEGIN
    IF EXISTS (
        SELECT 1 FROM clinic_app.audit_event
         WHERE organization_id <> '00000000-0000-0000-0000-000000000000'::uuid
           AND actor_user_id IS NULL
    ) THEN
        RAISE EXCEPTION 'machine expiry audit is populated; rollback requires restore'
            USING ERRCODE = '23514', CONSTRAINT = 'scheduling_expiry_audit_rollback';
    END IF;
END $guard$;
"""

REVERSE_SQL: Final = (
    _ROLLBACK_GUARD
    + "SET LOCAL ROLE clinic_resolver;\n"
    + EXPIRE_HOLD
    + """RESET ROLE;
DROP TRIGGER scheduling_appointment_expiry_audit
    ON clinic_app.scheduling_appointment;
DROP FUNCTION clinic_app.audit_appointment_expired();
"""
    + _HUMAN_ACTOR_SCOPE
)

# ruff: noqa: S608
"""Install trusted-context audit append functions and tenant view.

# noqa: SIZE_OK
"""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

INSTALL_PGCRYPTO_SQL = """
DO $guard$
DECLARE
    installed_schema pg_catalog.text;
BEGIN
    SELECT namespace.nspname
      INTO installed_schema
      FROM pg_catalog.pg_extension AS extension
      JOIN pg_catalog.pg_namespace AS namespace
        ON namespace.oid = extension.extnamespace
     WHERE extension.extname = 'pgcrypto';

    IF installed_schema IS NOT NULL AND installed_schema <> 'clinic_app' THEN
        RAISE EXCEPTION USING
            ERRCODE = '55000',
            MESSAGE = 'pgcrypto must be installed in clinic_app schema';
    END IF;
END
$guard$;

CREATE EXTENSION IF NOT EXISTS pgcrypto
    WITH SCHEMA clinic_app VERSION '1.3';
"""

DROP_PGCRYPTO_SQL = "DROP EXTENSION IF EXISTS pgcrypto"

TENANT_CONTEXT_SQL = """
    tenant_setting := NULLIF(
        pg_catalog.current_setting('app.current_tenant', true), ''
    );
    IF tenant_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        tenant_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_tenant is required';
    END IF;
    context_organization_id := tenant_setting::pg_catalog.uuid;
    IF context_organization_id =
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'system organization is not a tenant context';
    END IF;

    actor_setting := NULLIF(
        pg_catalog.current_setting('app.current_user_id', true), ''
    );
    IF actor_setting IS NULL OR NOT pg_catalog.pg_input_is_valid(
        actor_setting, 'uuid'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'valid app.current_user_id is required';
    END IF;
    context_actor_user_id := actor_setting::pg_catalog.uuid;
"""

SYSTEM_CONTEXT_SQL = """
    context_organization_id :=
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid;
    actor_setting := NULLIF(
        pg_catalog.current_setting('app.current_user_id', true), ''
    );
    IF actor_setting IS NOT NULL THEN
        IF NOT pg_catalog.pg_input_is_valid(
            actor_setting, 'uuid'
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '22023',
                MESSAGE = 'app.current_user_id is malformed';
        END IF;
        context_actor_user_id := actor_setting::pg_catalog.uuid;
    END IF;
"""


def append_function_sql(
    function_name: str,
    context_sql: str,
    privilege_sql: str,
) -> str:
    """Build one append function from the shared validation and lock contract."""
    return f"""
CREATE FUNCTION clinic_app.{function_name}(
    event_type pg_catalog.text,
    component_id pg_catalog.text,
    component_ip pg_catalog.inet,
    affected_record_type pg_catalog.text,
    affected_record_id pg_catalog.text,
    occurred_at_utc pg_catalog.timestamptz,
    payload pg_catalog.jsonb,
    content_hash pg_catalog.bytea
)
RETURNS bigint
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    tenant_setting pg_catalog.text;
    actor_setting pg_catalog.text;
    context_organization_id pg_catalog.uuid;
    context_actor_user_id pg_catalog.uuid;
    previous_hash pg_catalog.bytea;
    current_hash pg_catalog.bytea;
    captured_clock pg_catalog.timestamptz;
    inserted_seq bigint;
BEGIN
    IF pg_catalog.current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'audit append requires read committed isolation';
    END IF;

{context_sql}

    IF event_type IS NULL
        OR pg_catalog.char_length(pg_catalog.btrim(event_type)) NOT BETWEEN 1 AND 128
        OR component_id IS NULL
        OR pg_catalog.char_length(pg_catalog.btrim(component_id)) NOT BETWEEN 1 AND 255
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'event type and component identifier are required';
    END IF;
    IF (affected_record_type IS NULL) <> (affected_record_id IS NULL)
        OR (
            affected_record_type IS NOT NULL
            AND (
                pg_catalog.char_length(pg_catalog.btrim(affected_record_type))
                    NOT BETWEEN 1 AND 128
                OR pg_catalog.char_length(pg_catalog.btrim(affected_record_id))
                    NOT BETWEEN 1 AND 255
            )
        )
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'affected record fields must be a bounded pair';
    END IF;
    IF component_ip IS NOT NULL AND NOT (
        (pg_catalog.family(component_ip) = 4 AND pg_catalog.masklen(component_ip) = 32)
        OR
        (pg_catalog.family(component_ip) = 6 AND pg_catalog.masklen(component_ip) = 128)
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'component IP must be a host address';
    END IF;
    IF occurred_at_utc IS NULL OR NOT pg_catalog.isfinite(occurred_at_utc) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'finite occurrence timestamp is required';
    END IF;
    IF content_hash IS NULL OR pg_catalog.octet_length(content_hash) <> 32 THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'content hash must be 32 bytes';
    END IF;
    IF payload IS NULL OR pg_catalog.jsonb_typeof(payload) <> 'object' THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'audit payload must be an object';
    END IF;
    IF EXISTS (
        SELECT 1
          FROM pg_catalog.jsonb_each(payload) AS item(key, value)
         WHERE CASE
            WHEN item.key = 'http_status' THEN
                pg_catalog.jsonb_typeof(item.value) <> 'number'
                OR item.value::pg_catalog.text !~ '^[0-9][0-9][0-9]$'
                OR (item.value::pg_catalog.text)::integer
                    NOT BETWEEN 100 AND 599
            WHEN item.key IN (
                'http_method', 'object_verb', 'reason_code', 'request_id'
            ) THEN
                pg_catalog.jsonb_typeof(item.value) <> 'string'
                OR pg_catalog.char_length(
                    pg_catalog.btrim(item.value #>> '{{}}')
                ) NOT BETWEEN 1 AND CASE item.key
                    WHEN 'http_method' THEN 16
                    WHEN 'object_verb' THEN 64
                    ELSE 255
                END
            ELSE TRUE
         END
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'audit payload key or value is invalid';
    END IF;

    PERFORM pg_catalog.pg_advisory_xact_lock(
        pg_catalog.hashtextextended(
            ('clinic-audit:' || context_organization_id::pg_catalog.text)
                COLLATE pg_catalog."C",
            0::bigint
        )
    );
    captured_clock := pg_catalog.clock_timestamp();
    IF occurred_at_utc < captured_clock - pg_catalog.interval '5 minutes'
        OR occurred_at_utc > captured_clock + pg_catalog.interval '5 minutes'
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'occurrence timestamp is outside the five-minute window';
    END IF;

    SELECT audit_event.curr_hash
      INTO previous_hash
      FROM clinic_app.audit_event AS audit_event
     WHERE audit_event.organization_id = context_organization_id
     ORDER BY audit_event.seq DESC
     LIMIT 1;
    IF previous_hash IS NULL THEN
        previous_hash := pg_catalog.decode(pg_catalog.repeat('00', 32), 'hex');
    END IF;
    current_hash := clinic_app.digest(content_hash || previous_hash, 'sha256');

    INSERT INTO clinic_app.audit_event (
        organization_id, actor_user_id, event_type, component_id, component_ip,
        affected_record_type, affected_record_id, occurred_at_utc, payload,
        prev_hash, curr_hash
    ) VALUES (
        context_organization_id, context_actor_user_id,
        event_type, component_id, component_ip,
        affected_record_type, affected_record_id, occurred_at_utc, payload,
        previous_hash, current_hash
    )
    RETURNING seq INTO inserted_seq;
    RETURN inserted_seq;
END
$function$;

ALTER FUNCTION clinic_app.{function_name}(
    pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
    pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
) OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.{function_name}(
    pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
    pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
) FROM PUBLIC, clinic_app, clinic_resolver;
{privilege_sql}
"""


TENANT_APPEND_SQL = append_function_sql(
    "audit_append",
    TENANT_CONTEXT_SQL,
    """GRANT EXECUTE ON FUNCTION clinic_app.audit_append(
    pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
    pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
) TO clinic_app;""",
)

SYSTEM_APPEND_SQL = append_function_sql(
    "audit_append_system",
    SYSTEM_CONTEXT_SQL,
    "",
)

VIEW_SQL = """
CREATE VIEW clinic_app.audit_event_tenant (
    seq, organization_id, actor_user_id, event_type, component_id, component_ip,
    affected_record_type, affected_record_id, occurred_at_utc, payload,
    prev_hash, curr_hash
) WITH (security_barrier = true, security_invoker = false) AS
SELECT
    audit_event.seq,
    audit_event.organization_id,
    audit_event.actor_user_id,
    audit_event.event_type,
    audit_event.component_id,
    audit_event.component_ip,
    audit_event.affected_record_type,
    audit_event.affected_record_id,
    audit_event.occurred_at_utc,
    audit_event.payload,
    audit_event.prev_hash,
    audit_event.curr_hash
FROM clinic_app.audit_event AS audit_event
WHERE audit_event.organization_id <>
        '00000000-0000-0000-0000-000000000000'::pg_catalog.uuid
  AND audit_event.organization_id = CASE
        WHEN pg_catalog.pg_input_is_valid(
            NULLIF(
                pg_catalog.current_setting('app.current_tenant', true), ''
            ),
            'uuid'
        )
        THEN NULLIF(
            pg_catalog.current_setting('app.current_tenant', true), ''
        )::pg_catalog.uuid
        ELSE NULL::pg_catalog.uuid
      END;

ALTER VIEW clinic_app.audit_event_tenant OWNER TO clinic_owner;
REVOKE ALL ON TABLE clinic_app.audit_event_tenant
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT SELECT ON TABLE clinic_app.audit_event_tenant TO clinic_app;
"""

DROP_AUDIT_API_SQL = """
DROP VIEW IF EXISTS clinic_app.audit_event_tenant;
DROP FUNCTION IF EXISTS clinic_app.audit_append_system(
    pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
    pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
);
DROP FUNCTION IF EXISTS clinic_app.audit_append(
    pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
    pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
);
"""


class Migration(migrations.Migration):
    """Install the pgcrypto-backed trusted append API and tenant view."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0001_initial"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            sql=INSTALL_PGCRYPTO_SQL,
            reverse_sql=DROP_PGCRYPTO_SQL,
        ),
        migrations.RunSQL(
            sql=TENANT_APPEND_SQL + SYSTEM_APPEND_SQL + VIEW_SQL,
            reverse_sql=DROP_AUDIT_API_SQL,
        ),
    ]

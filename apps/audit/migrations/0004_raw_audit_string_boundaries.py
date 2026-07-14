# ruff: noqa: S608
"""Reject audit strings that PostgreSQL cannot persist byte-for-byte."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

FUNCTION_ARGUMENTS = """
    pg_catalog.text, pg_catalog.text, pg_catalog.inet, pg_catalog.text,
    pg_catalog.text, pg_catalog.timestamptz, pg_catalog.jsonb, pg_catalog.bytea
"""


def boundary_wrapper_sql(
    function_name: str,
    inner_name: str,
    privilege_sql: str,
) -> str:
    """Wrap one existing append function with raw string-domain guards."""
    return f"""
ALTER FUNCTION clinic_app.{function_name}({FUNCTION_ARGUMENTS})
    RENAME TO {inner_name};
ALTER FUNCTION clinic_app.{inner_name}({FUNCTION_ARGUMENTS})
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.{inner_name}({FUNCTION_ARGUMENTS})
    FROM PUBLIC, clinic_app, clinic_resolver;

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
    whitespace_chars CONSTANT pg_catalog.text :=
        pg_catalog.chr(9) || pg_catalog.chr(10) || pg_catalog.chr(11)
        || pg_catalog.chr(12) || pg_catalog.chr(13)
        || pg_catalog.chr(28) || pg_catalog.chr(29)
        || pg_catalog.chr(30) || pg_catalog.chr(31)
        || pg_catalog.chr(32) || pg_catalog.chr(133)
        || pg_catalog.chr(160) || pg_catalog.chr(5760)
        || pg_catalog.chr(8192) || pg_catalog.chr(8193)
        || pg_catalog.chr(8194) || pg_catalog.chr(8195)
        || pg_catalog.chr(8196) || pg_catalog.chr(8197)
        || pg_catalog.chr(8198) || pg_catalog.chr(8199)
        || pg_catalog.chr(8200) || pg_catalog.chr(8201)
        || pg_catalog.chr(8202) || pg_catalog.chr(8232)
        || pg_catalog.chr(8233) || pg_catalog.chr(8239)
        || pg_catalog.chr(8287) || pg_catalog.chr(12288);
BEGIN
    IF event_type IS NULL
        OR pg_catalog.btrim(event_type, whitespace_chars) = ''
        OR pg_catalog.char_length(event_type) > 128
        OR component_id IS NULL
        OR pg_catalog.btrim(component_id, whitespace_chars) = ''
        OR pg_catalog.char_length(component_id) > 255
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'event type and component identifier are required';
    END IF;
    IF (affected_record_type IS NULL) <> (affected_record_id IS NULL)
        OR (
            affected_record_type IS NOT NULL
            AND (
                pg_catalog.btrim(affected_record_type, whitespace_chars) = ''
                OR pg_catalog.char_length(affected_record_type) > 128
                OR pg_catalog.btrim(affected_record_id, whitespace_chars) = ''
                OR pg_catalog.char_length(affected_record_id) > 255
            )
        )
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'affected record fields must be a bounded pair';
    END IF;
    IF payload IS NOT NULL
        AND pg_catalog.jsonb_typeof(payload) = 'object'
        AND EXISTS (
            SELECT 1
              FROM pg_catalog.jsonb_each(payload) AS item(key, value)
             WHERE item.key IN (
                    'http_method', 'object_verb', 'reason_code', 'request_id'
                )
               AND pg_catalog.jsonb_typeof(item.value) = 'string'
               AND (
                    pg_catalog.btrim(
                        item.value #>> '{{}}', whitespace_chars
                    ) = ''
                    OR pg_catalog.char_length(item.value #>> '{{}}') >
                        CASE item.key
                            WHEN 'http_method' THEN 16
                            WHEN 'object_verb' THEN 64
                            ELSE 255
                        END
               )
        )
    THEN
        RAISE EXCEPTION USING ERRCODE = '22023',
            MESSAGE = 'audit payload key or value is invalid';
    END IF;

    RETURN clinic_app.{inner_name}(
        event_type, component_id, component_ip,
        affected_record_type, affected_record_id,
        occurred_at_utc, payload, content_hash
    );
END
$function$;

ALTER FUNCTION clinic_app.{function_name}({FUNCTION_ARGUMENTS})
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.{function_name}({FUNCTION_ARGUMENTS})
    FROM PUBLIC, clinic_app, clinic_resolver;
{privilege_sql}
"""


TENANT_WRAPPER_SQL = boundary_wrapper_sql(
    "audit_append",
    "audit_append_unchecked_v1",
    f"""GRANT EXECUTE ON FUNCTION clinic_app.audit_append(
{FUNCTION_ARGUMENTS}
) TO clinic_app;""",
)

SYSTEM_WRAPPER_SQL = boundary_wrapper_sql(
    "audit_append_system",
    "audit_append_system_unchecked_v1",
    "",
)

INSTALL_BOUNDARIES_SQL = TENANT_WRAPPER_SQL + SYSTEM_WRAPPER_SQL

REMOVE_BOUNDARIES_SQL = f"""
DROP FUNCTION clinic_app.audit_append({FUNCTION_ARGUMENTS});
DROP FUNCTION clinic_app.audit_append_system({FUNCTION_ARGUMENTS});

ALTER FUNCTION clinic_app.audit_append_unchecked_v1({FUNCTION_ARGUMENTS})
    RENAME TO audit_append;
ALTER FUNCTION clinic_app.audit_append_system_unchecked_v1({FUNCTION_ARGUMENTS})
    RENAME TO audit_append_system;

ALTER FUNCTION clinic_app.audit_append({FUNCTION_ARGUMENTS})
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.audit_append({FUNCTION_ARGUMENTS})
    FROM PUBLIC, clinic_app, clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.audit_append({FUNCTION_ARGUMENTS})
    TO clinic_app;

ALTER FUNCTION clinic_app.audit_append_system({FUNCTION_ARGUMENTS})
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.audit_append_system({FUNCTION_ARGUMENTS})
    FROM PUBLIC, clinic_app, clinic_resolver;
"""


class Migration(migrations.Migration):
    """Install reversible raw-domain guards around both append paths."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0003_immutability_and_verification"),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            sql=INSTALL_BOUNDARIES_SQL,
            reverse_sql=REMOVE_BOUNDARIES_SQL,
        ),
    ]

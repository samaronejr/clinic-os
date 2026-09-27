"""Tenant-DEK HMAC indexes, matching the todo-17 envelope boundary."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

INSTALL_SQL = """
CREATE FUNCTION clinic_app.protected_blind_index(
    kek pg_catalog.text, purpose pg_catalog.text, plaintext pg_catalog.bytea
)
RETURNS TABLE(blind_index pg_catalog.bytea, key_version pg_catalog.int4)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    prior_tenant pg_catalog.text;
    resolved pg_catalog.uuid;
    key_row pg_catalog.record;
BEGIN
    resolved := clinic_app.protected_tenant();
    prior_tenant := pg_catalog.current_setting('app.current_tenant', true);
    PERFORM pg_catalog.set_config('app.current_tenant', resolved::text, true);
    IF kek IS NULL OR NOT kek ~ '^[0-9a-f]{64}$' THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid wrapping key';
    END IF;
    IF purpose IS NULL OR pg_catalog.char_length(purpose) NOT BETWEEN 1 AND 128
        OR purpose ~ '[[:cntrl:]]' OR plaintext IS NULL THEN
        RAISE EXCEPTION USING ERRCODE = '22023', MESSAGE = 'invalid index input';
    END IF;
    FOR key_row IN
        SELECT version_row.key_version
        FROM clinic_app.tenancy_tenantdatakey AS version_row
        WHERE version_row.organization_id = resolved
        ORDER BY version_row.key_version
    LOOP
        blind_index := clinic_app.hmac(
            pg_catalog.convert_to(purpose, 'UTF8') || pg_catalog.decode('00', 'hex')
                || plaintext,
            clinic_app.tenant_dek_unwrap(kek, key_row.key_version), 'sha256'
        );
        key_version := key_row.key_version;
        RETURN NEXT;
    END LOOP;
    PERFORM pg_catalog.set_config(
        'app.current_tenant', COALESCE(prior_tenant, ''), true
    );
END
$function$;
ALTER FUNCTION clinic_app.protected_blind_index(text, text, bytea)
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.protected_blind_index(text, text, bytea)
    FROM PUBLIC, clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.protected_blind_index(text, text, bytea)
    TO clinic_app;
"""


class Migration(migrations.Migration):
    """Install only the fixed purpose-bound digest API; keys never leave SQL."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("tenancy", "0006_runtime_pooling")
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(
            INSTALL_SQL,
            "DROP FUNCTION clinic_app.protected_blind_index(text, text, bytea);",
        )
    ]

"""Bind hash revocation to the authenticated account that owns the session."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

SQL = """
SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.identity_session_revoke_hash(requested_hash bytea)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH revoked AS (
        UPDATE clinic_app.identity_sessionrecord
        SET revoked_at = statement_timestamp()
        WHERE session_key_hash = requested_hash AND revoked_at IS NULL
          AND user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
          AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
        RETURNING id
    )
    SELECT EXISTS (SELECT 1 FROM revoked)
$function$;
RESET ROLE;
"""


class Migration(migrations.Migration):
    """Keep session tombstoning additive; identity and grants are unchanged."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0026_session_rotation_cause"),
    ]

    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL)]

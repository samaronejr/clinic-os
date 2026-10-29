"""Keep deleted explicitly revoked keys distinct from stale rotated keys."""

from typing import Final

from ._lifecycle_sessions_sql import SESSION_SQL

GUARD_SQL: Final = (
    "CREATE OR REPLACE FUNCTION clinic_app.identity_session_change_guard()"
    + SESSION_SQL.split(
        "CREATE FUNCTION clinic_app.identity_session_change_guard()", 1
    )[1]
    .split("$function$;", 1)[0]
    .replace(
        "OR NEW.verified_at IS DISTINCT FROM OLD.verified_at",
        "OR NEW.verified_at IS DISTINCT FROM OLD.verified_at\n"
        "           OR NEW.superseded_at IS DISTINCT FROM OLD.superseded_at",
    )
    + "$function$;\n"
)

SQL: Final = """
CREATE FUNCTION clinic_app.identity_session_rotate_hash(requested_hash bytea)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH superseded AS (
        UPDATE clinic_app.identity_sessionrecord
        SET superseded_at = statement_timestamp(),
            revoked_at = statement_timestamp()
        WHERE session_key_hash = requested_hash AND revoked_at IS NULL
          AND user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
          AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
        RETURNING id
    )
    SELECT EXISTS (SELECT 1 FROM superseded)
$function$;

CREATE FUNCTION clinic_app.identity_session_revoked_cookie(requested_hash bytea)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM clinic_app.identity_sessionrecord
        WHERE session_key_hash = requested_hash AND revoked_at IS NOT NULL
          AND superseded_at IS NULL
    )
$function$;
ALTER FUNCTION clinic_app.identity_session_rotate_hash(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_session_revoked_cookie(bytea)
    OWNER TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_session_rotate_hash(bytea),
    clinic_app.identity_session_revoked_cookie(bytea)
    FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_rotate_hash(bytea),
    clinic_app.identity_session_revoked_cookie(bytea)
    TO clinic_app;
RESET ROLE;
"""

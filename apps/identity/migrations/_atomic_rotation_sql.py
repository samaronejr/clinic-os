"""Rotate a live session key in one locked step, or register nothing.

A request admitted on a session (one unlocked ``identity_session_active`` read)
could rotate its key after a revocation committed: the old two-step path
superseded nothing, ignored that, and registered the new key as live. This
function takes the per-user lock ``identity_revoke_changed_sessions`` takes, so
a membership revocation either commits first (and the old key is no longer
live) or waits and then sees, and revokes, the new key.

``identity_session_rotate_hash`` stays installed but has no caller: the SQL
inventories pin every migration-defined function name to the live catalog,
and it can only revoke the caller's own live session.
"""

from typing import Final

from ._delegate_access_sql import _POPULATED

SQL: Final = """
CREATE FUNCTION clinic_app.identity_session_rotate(
    old_hash bytea, new_hash bytea, requested_network text,
    requested_family text, requested_expiry timestamptz
)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    actor uuid := NULLIF(current_setting('app.current_user_id', true), '')::uuid;
    result uuid;
BEGIN
    IF actor IS NULL OR old_hash IS NULL OR octet_length(old_hash) <> 32
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
    THEN
        RETURN NULL;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'clinic-lock-v1:user:' || actor::text, 0
    ));
    -- One block: a refusal after the supersede (register declined) undoes it.
    BEGIN
        UPDATE clinic_app.identity_sessionrecord
        SET superseded_at = statement_timestamp(),
            revoked_at = statement_timestamp()
        WHERE session_key_hash = old_hash AND user_id = actor
          AND revoked_at IS NULL AND expires_at > statement_timestamp();
        IF NOT FOUND THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001';
        END IF;
        result := clinic_app.identity_session_register(
            actor, new_hash, requested_network, requested_family, requested_expiry
        );
        IF result IS NULL THEN
            RAISE EXCEPTION USING ERRCODE = 'P0001';
        END IF;
    EXCEPTION WHEN raise_exception THEN
        RETURN NULL;
    END;
    RETURN result;
END
$function$;
ALTER FUNCTION clinic_app.identity_session_rotate(
    bytea, bytea, text, text, timestamptz
) OWNER TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_session_rotate(
    bytea, bytea, text, text, timestamptz
) FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_rotate(
    bytea, bytea, text, text, timestamptz
) TO clinic_app;
RESET ROLE;
"""

# Like 0053's reverse: a populated enterprise install refuses before any change,
# so the 0051 rollback refusal behind it still leaves the catalog untouched.
# The resolver reads the tables past FORCE RLS, as 0053's check does.
REVERSE_SQL: Final = """
SET LOCAL ROLE clinic_resolver;
DO $populated$
BEGIN
 IF __POPULATED__ THEN
 RAISE EXCEPTION 'populated enterprise identity rolls back only by restore'
 USING ERRCODE='55000';
 END IF;
END $populated$;
DROP FUNCTION clinic_app.identity_session_rotate(
    bytea, bytea, text, text, timestamptz
);
RESET ROLE;
""".replace("__POPULATED__", _POPULATED)

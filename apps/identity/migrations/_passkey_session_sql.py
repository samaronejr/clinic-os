"""Persist exact verified passkey sessions and recheck current organization policy."""

from typing import Final

PASSKEY_SESSION_SQL: Final = """
CREATE OR REPLACE FUNCTION clinic_app.identity_session_change_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.user_id IS DISTINCT FROM OLD.user_id
       OR NEW.session_key_hash IS DISTINCT FROM OLD.session_key_hash
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.ip_network IS DISTINCT FROM OLD.ip_network
       OR NEW.user_agent_family IS DISTINCT FROM OLD.user_agent_family
       OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
       OR NEW.superseded_at IS DISTINCT FROM OLD.superseded_at
          AND current_user <> 'clinic_resolver'
       OR (OLD.revoked_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'session identity is immutable';
    END IF;
    IF (
        NEW.verification_device_id IS DISTINCT FROM OLD.verification_device_id
        OR NEW.verification_passkey_id IS DISTINCT FROM OLD.verification_passkey_id
        OR NEW.verified_at IS DISTINCT FROM OLD.verified_at
    ) AND current_user <> 'clinic_resolver' THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'session verification requires its authenticated boundary';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION clinic_app.identity_session_verify_passkey(
    requested_hash bytea, requested_passkey uuid
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH verified AS (
        UPDATE clinic_app.identity_sessionrecord AS session
        SET verification_device_id = NULL,
            verification_passkey_id = credential.id,
            verified_at = statement_timestamp()
        FROM clinic_app.identity_passkey AS credential
        WHERE session.session_key_hash = requested_hash
          AND session.user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND session.revoked_at IS NULL
          AND session.expires_at > statement_timestamp()
          AND credential.id = requested_passkey
          AND credential.user_id = session.user_id
          AND credential.organization_id = NULLIF(
              current_setting('app.current_tenant', true), ''
          )::uuid
          AND credential.removed_at IS NULL
          AND credential.last_used >= statement_timestamp() - interval '5 minutes'
          AND credential.last_used <= statement_timestamp()
          AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
          AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
          AND clinic_app.user_has_org(credential.organization_id)
        RETURNING session.id
    )
    SELECT EXISTS (SELECT 1 FROM verified)
$function$;

CREATE FUNCTION clinic_app.identity_passkey_session_verified(
    requested_hash bytea, requested_passkey uuid, requested_age integer
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM clinic_app.identity_sessionrecord AS session
        JOIN clinic_app.identity_passkey AS credential
          ON credential.id = session.verification_passkey_id
        JOIN clinic_app.identity_organization AS organization
          ON organization.id = credential.organization_id
        JOIN clinic_app.identity_user AS account ON account.id = session.user_id
        WHERE session.session_key_hash = requested_hash
          AND session.user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND session.revoked_at IS NULL
          AND session.expires_at > statement_timestamp()
          AND session.verification_device_id IS NULL
          AND session.verified_at <= statement_timestamp()
          AND (
              requested_age IS NULL
              OR (requested_age >= 0 AND session.verified_at >=
                  statement_timestamp() - requested_age * interval '1 second')
          )
          AND credential.id = requested_passkey
          AND credential.user_id = session.user_id
          AND credential.organization_id = NULLIF(
              current_setting('app.current_tenant', true), ''
          )::uuid
          AND credential.removed_at IS NULL
          AND organization.passkey_step_up_enabled AND organization.status = 'active'
          AND account.is_active
          AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
          AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
          AND clinic_app.user_has_org(credential.organization_id)
    )
$function$;
ALTER FUNCTION clinic_app.identity_session_verify_passkey(bytea, uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_passkey_session_verified(bytea, uuid, integer)
    OWNER TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_session_verify_passkey(bytea, uuid)
    FROM PUBLIC, clinic_app, clinic_agent;
REVOKE ALL ON FUNCTION
    clinic_app.identity_passkey_session_verified(bytea, uuid, integer)
    FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_verify_passkey(bytea, uuid)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION
    clinic_app.identity_passkey_session_verified(bytea, uuid, integer)
    TO clinic_app;
RESET ROLE;
"""

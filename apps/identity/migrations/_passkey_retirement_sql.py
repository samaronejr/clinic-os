"""Passkey removal/reset retirement and mutually exclusive TOTP verification."""

from typing import Final

RETIREMENT_SQL: Final = """
CREATE FUNCTION clinic_app.identity_passkey_revoke_sessions()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF OLD.removed_at IS NULL AND NEW.removed_at IS NOT NULL THEN
        UPDATE clinic_app.identity_sessionrecord
        SET revoked_at = statement_timestamp()
        WHERE verification_passkey_id = NEW.id AND revoked_at IS NULL;
    END IF;
    RETURN NEW;
END
$function$;
ALTER FUNCTION clinic_app.identity_passkey_revoke_sessions()
    OWNER TO clinic_resolver;
CREATE TRIGGER identity_passkey_session_rejection
    AFTER UPDATE OF removed_at ON clinic_app.identity_passkey
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_passkey_revoke_sessions();
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_passkey_revoke_sessions()
    FROM PUBLIC, clinic_app, clinic_agent;
RESET ROLE;

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.identity_retire_authenticators(
    requested_user uuid
)
RETURNS void LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    INSERT INTO clinic_app.identity_authenticatorretirement(
        id, user_id, device_id, retired_at
    )
    SELECT gen_random_uuid(), user_id, id, statement_timestamp()
    FROM clinic_app.otp_totp_totpdevice WHERE user_id = requested_user
    ON CONFLICT (device_id) DO NOTHING;
    UPDATE clinic_app.identity_passkey SET removed_at = statement_timestamp()
    WHERE user_id = requested_user AND removed_at IS NULL;
    UPDATE clinic_app.identity_recoverybatch
    SET revoked_at = statement_timestamp()
    WHERE user_id = requested_user AND revoked_at IS NULL;
    UPDATE clinic_app.identity_sessionrecord
    SET revoked_at = statement_timestamp()
    WHERE user_id = requested_user AND revoked_at IS NULL;
END
$function$;

CREATE OR REPLACE FUNCTION clinic_app.identity_session_verify(
    requested_hash bytea, requested_device integer
)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH verified AS (
        UPDATE clinic_app.identity_sessionrecord AS session
        SET verification_device_id = requested_device,
            verification_passkey_id = NULL,
            verified_at = statement_timestamp()
        FROM clinic_app.otp_totp_totpdevice AS device
        WHERE session.session_key_hash = requested_hash
          AND session.user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND session.revoked_at IS NULL
          AND session.expires_at > statement_timestamp()
          AND device.id = requested_device AND device.user_id = session.user_id
          AND device.confirmed AND device.last_t >= 0
          AND device.last_used_at >= statement_timestamp() - interval '5 minutes'
          AND NOT EXISTS (
              SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
              WHERE retirement.device_id = device.id
          )
        RETURNING session.id
    )
    SELECT EXISTS (SELECT 1 FROM verified)
$function$;
RESET ROLE;
"""

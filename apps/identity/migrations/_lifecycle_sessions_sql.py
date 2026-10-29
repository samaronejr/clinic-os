"""Purpose-bound session inventory and immediate password/membership rejection."""

from typing import Final

SESSION_SQL: Final = """
CREATE FUNCTION clinic_app.identity_session_register(
    requested_user uuid, requested_hash bytea, requested_network text,
    requested_family text, requested_expiry timestamptz
)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    result uuid;
BEGIN
    IF requested_hash IS NULL OR octet_length(requested_hash) <> 32
       OR requested_user IS DISTINCT FROM NULLIF(
           current_setting('app.current_user_id', true), ''
       )::uuid
       OR requested_expiry IS NULL OR requested_expiry <= statement_timestamp()
       OR requested_network IS NULL OR requested_family IS NULL
       OR requested_family NOT IN ('chromium', 'firefox', 'safari', 'other')
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
       OR NOT EXISTS (
           SELECT 1 FROM clinic_app.identity_user
           WHERE id = requested_user AND is_active
       )
    THEN
        RETURN NULL;
    END IF;
    INSERT INTO clinic_app.identity_sessionrecord (
        id, user_id, session_key_hash, created_at, last_seen, ip_network,
        user_agent_family, expires_at, revoked_at, verification_device_id, verified_at
    ) VALUES (
        gen_random_uuid(), requested_user, requested_hash, statement_timestamp(),
        statement_timestamp(), requested_network, requested_family, requested_expiry,
        NULL, NULL, NULL
    )
    ON CONFLICT (session_key_hash) DO NOTHING
    RETURNING id INTO result;
    IF result IS NULL THEN
        SELECT id INTO result FROM clinic_app.identity_sessionrecord
        WHERE session_key_hash = requested_hash AND user_id = requested_user
          AND revoked_at IS NULL AND expires_at > statement_timestamp();
    END IF;
    RETURN result;
END
$function$;

CREATE FUNCTION clinic_app.identity_session_active(
    requested_user uuid, requested_hash bytea
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM clinic_app.identity_sessionrecord AS session
        JOIN clinic_app.identity_user AS account ON account.id = session.user_id
        WHERE session.user_id = requested_user
          AND session.session_key_hash = requested_hash
          AND session.revoked_at IS NULL
          AND session.expires_at > statement_timestamp()
          AND account.is_active
    )
$function$;

CREATE FUNCTION clinic_app.identity_session_revoke_hash(requested_hash bytea)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH revoked AS (
        UPDATE clinic_app.identity_sessionrecord
        SET revoked_at = statement_timestamp()
        WHERE session_key_hash = requested_hash AND revoked_at IS NULL
        RETURNING id
    )
    SELECT EXISTS (SELECT 1 FROM revoked)
$function$;

CREATE FUNCTION clinic_app.identity_session_verify(
    requested_hash bytea, requested_device integer
)
RETURNS boolean LANGUAGE sql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH verified AS (
        UPDATE clinic_app.identity_sessionrecord AS session
        SET verification_device_id = requested_device,
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

CREATE FUNCTION clinic_app.identity_session_verified(
    requested_hash bytea
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT EXISTS (
        SELECT 1 FROM clinic_app.identity_sessionrecord AS session
        JOIN clinic_app.otp_totp_totpdevice AS device
          ON device.id = session.verification_device_id
        JOIN clinic_app.identity_user AS account ON account.id = session.user_id
        WHERE session.user_id = NULLIF(
            current_setting('app.current_user_id', true), ''
        )::uuid
          AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
          AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
          AND session.session_key_hash = requested_hash
          AND session.revoked_at IS NULL
          AND session.expires_at > statement_timestamp()
          AND session.verified_at >= statement_timestamp() - interval '5 minutes'
          AND session.verified_at <= statement_timestamp()
          AND device.user_id = session.user_id AND device.confirmed
          AND account.is_active
          AND NOT EXISTS (
              SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
              WHERE retirement.device_id = device.id
          )
    )
$function$;

CREATE FUNCTION clinic_app.identity_session_step_up(
    requested_hash bytea, requested_clinic uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT clinic_app.identity_session_verified(requested_hash) AND (
        clinic_app.has_permission('staff.clinic', requested_clinic, NULL)
        OR clinic_app.has_permission('staff.organization', requested_clinic, NULL)
    )
$function$;

CREATE FUNCTION clinic_app.identity_session_change_guard()
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
       OR (OLD.revoked_at IS NOT NULL AND NEW IS DISTINCT FROM OLD)
    THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'session identity is immutable';
    END IF;
    IF (
           NEW.verification_device_id IS DISTINCT FROM OLD.verification_device_id
           OR NEW.verified_at IS DISTINCT FROM OLD.verified_at
       )
       AND current_user <> 'clinic_resolver'
    THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'session verification requires its authenticated boundary';
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION clinic_app.identity_revoke_changed_sessions()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    changed_user uuid;
BEGIN
    IF TG_TABLE_NAME = 'identity_user' THEN
        IF OLD.password IS NOT DISTINCT FROM NEW.password
           AND OLD.is_active IS NOT DISTINCT FROM NEW.is_active
        THEN
            RETURN NEW;
        END IF;
        changed_user := NEW.id;
    ELSIF TG_TABLE_NAME = 'identity_rolegrant' THEN
        UPDATE clinic_app.identity_sessionrecord AS session
        SET revoked_at = statement_timestamp()
        WHERE session.revoked_at IS NULL AND EXISTS (
            SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
            WHERE membership.user_id = session.user_id
              AND membership.revoked_at IS NULL
              AND (
                  (membership.clinic_id = NEW.clinic_id AND membership.role = NEW.role)
                  OR (
                      TG_OP = 'UPDATE' AND membership.clinic_id = OLD.clinic_id
                      AND membership.role = OLD.role
                  )
              )
        );
        RETURN NEW;
    ELSE
        PERFORM pg_advisory_xact_lock(hashtextextended(
            'clinic-lock-v1:user:' || NEW.user_id::text, 0
        ));
        changed_user := NEW.user_id;
        IF TG_OP = 'UPDATE' AND OLD.user_id IS DISTINCT FROM NEW.user_id THEN
            UPDATE clinic_app.identity_sessionrecord
            SET revoked_at = statement_timestamp()
            WHERE user_id = OLD.user_id AND revoked_at IS NULL;
        END IF;
    END IF;
    UPDATE clinic_app.identity_sessionrecord
    SET revoked_at = statement_timestamp()
    WHERE user_id = changed_user AND revoked_at IS NULL;
    RETURN NEW;
END
$function$;

ALTER FUNCTION clinic_app.identity_session_register(
    uuid, bytea, text, text, timestamptz
) OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_session_active(uuid, bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_session_revoke_hash(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_session_verify(bytea, integer)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_session_verified(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_session_step_up(bytea, uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_revoke_changed_sessions()
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_session_register(
    uuid, bytea, text, text, timestamptz
) FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_session_active(uuid, bytea)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_session_revoke_hash(bytea)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_session_verify(bytea, integer)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_session_verified(bytea)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_session_step_up(bytea, uuid)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_revoke_changed_sessions()
    FROM PUBLIC, clinic_app, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_session_change_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_register(
    uuid, bytea, text, text, timestamptz
) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_active(uuid, bytea)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_revoke_hash(bytea)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_verify(bytea, integer)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_verified(bytea)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_step_up(bytea, uuid)
    TO clinic_app;

CREATE TRIGGER identity_session_change
    BEFORE UPDATE ON clinic_app.identity_sessionrecord
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_session_change_guard();
CREATE TRIGGER identity_password_session_rejection
    AFTER UPDATE OF password, is_active ON clinic_app.identity_user
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_revoke_changed_sessions();
CREATE TRIGGER identity_membership_session_rejection
    BEFORE INSERT OR UPDATE ON clinic_app.identity_userclinicrole
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_revoke_changed_sessions();
CREATE TRIGGER identity_rolegrant_session_rejection
    AFTER INSERT OR UPDATE ON clinic_app.identity_rolegrant
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_revoke_changed_sessions();
REVOKE INSERT ON clinic_app.identity_sessionrecord FROM clinic_app;

CREATE POLICY identity_totp_retirement
    ON clinic_app.otp_totp_totpdevice AS RESTRICTIVE FOR ALL TO clinic_app
    USING (
        NOT EXISTS (
            SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
            WHERE retirement.device_id = otp_totp_totpdevice.id
        )
    )
    WITH CHECK (
        NOT EXISTS (
            SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
            WHERE retirement.device_id = otp_totp_totpdevice.id
        )
    );
"""

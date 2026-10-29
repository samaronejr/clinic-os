"""Password and real confirmed-TOTP boundary for protected invitations."""

from typing import Final

INVITATION_SQL: Final = """
CREATE FUNCTION clinic_app.identity_invitation_user(
    requested_clinic uuid, requested_user uuid
)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
       OR NOT (
        clinic_app.has_permission('staff.clinic', requested_clinic, NULL)
        OR clinic_app.has_permission('staff.organization', requested_clinic, NULL)
    ) THEN
        RETURN NULL;
    END IF;
    INSERT INTO clinic_app.identity_user(
        id, password, last_login, is_superuser, username, first_name, last_name,
        email, is_staff, is_active, date_joined
    ) VALUES (
        requested_user, '!', NULL, false, 'invitation_' || requested_user::text,
        '', '', '', false, false, statement_timestamp()
    );
    RETURN requested_user;
END
$function$;

CREATE FUNCTION clinic_app.identity_invitation_claim(requested_hash bytea)
RETURNS TABLE (
    invitation_id uuid, organization_id uuid, clinic_id uuid, user_id uuid,
    desired_username bytea, encoded_password text, device_id integer
)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NULLIF(current_setting('app.current_user_id', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_tenant', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
    THEN
        RETURN;
    END IF;
    RETURN QUERY
        SELECT invitation.id, invitation.organization_id, invitation.clinic_id,
            invitation.pending_user_id, invitation.desired_username,
            account.password, invitation.device_id
        FROM clinic_app.identity_staffinvitation AS invitation
        JOIN clinic_app.identity_user AS account
          ON account.id = invitation.pending_user_id
        JOIN clinic_app.identity_organization AS organization
          ON organization.id = invitation.organization_id
        WHERE invitation.token_hash = requested_hash
          AND invitation.state = 'pending' AND invitation.revoked_at IS NULL
          AND invitation.expires_at > statement_timestamp()
          AND organization.status = 'active' AND NOT account.is_active
        FOR UPDATE OF invitation, account;
END
$function$;

CREATE FUNCTION clinic_app.identity_invitation_begin(
    requested_hash bytea, requested_username bytea,
    requested_password text, requested_device_key text
)
RETURNS integer LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    invitation clinic_app.identity_staffinvitation%ROWTYPE;
    created_device integer;
BEGIN
    SELECT * INTO invitation
    FROM clinic_app.identity_staffinvitation
    WHERE token_hash = requested_hash
      AND state = 'pending' AND revoked_at IS NULL
      AND expires_at > statement_timestamp()
      AND organization_id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
      AND pending_user_id = NULLIF(
          current_setting('app.current_user_id', true), ''
      )::uuid
    FOR UPDATE;
    IF NOT FOUND OR invitation.password_set_at IS NOT NULL
       OR requested_username IS NULL OR requested_password IS NULL
       OR requested_password = '!' OR requested_device_key IS NULL
       OR requested_device_key !~ '^[a-f0-9]{40}$'
    THEN
        RETURN NULL;
    END IF;
    UPDATE clinic_app.identity_user
    SET password = requested_password
    WHERE id = invitation.pending_user_id AND NOT is_active;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    INSERT INTO clinic_app.otp_totp_totpdevice(
        user_id, name, confirmed, key, step, t0, digits, tolerance,
        drift, last_t, throttling_failure_timestamp, throttling_failure_count,
        created_at, last_used_at
    ) VALUES (
        invitation.pending_user_id, 'Clinic OS authenticator', false,
        requested_device_key, 30, 0, 6, 1, 0, -1, NULL, 0,
        statement_timestamp(), NULL
    ) RETURNING id INTO created_device;
    UPDATE clinic_app.identity_staffinvitation
    SET desired_username = requested_username,
        password_set_at = statement_timestamp(),
        device_id = created_device
    WHERE id = invitation.id;
    RETURN created_device;
END
$function$;

CREATE FUNCTION clinic_app.identity_invitation_finish(
    requested_hash bytea, requested_username text
)
RETURNS uuid LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    invitation clinic_app.identity_staffinvitation%ROWTYPE;
    device_user uuid;
    sender_authorized boolean;
    old_actor text;
BEGIN
    SELECT * INTO invitation FROM clinic_app.identity_staffinvitation
    WHERE token_hash = requested_hash
      AND state = 'pending' AND revoked_at IS NULL
      AND expires_at > statement_timestamp()
      AND organization_id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
      AND pending_user_id = NULLIF(
          current_setting('app.current_user_id', true), ''
      )::uuid
    FOR UPDATE;
    IF NOT FOUND OR invitation.password_set_at IS NULL
       OR invitation.desired_username IS NULL
       OR requested_username IS NULL OR requested_username = ''
    THEN
        RETURN NULL;
    END IF;
    SELECT user_id INTO device_user
    FROM clinic_app.otp_totp_totpdevice AS device
    WHERE device.id = invitation.device_id AND confirmed AND last_t >= 0
      AND last_used_at >= statement_timestamp() - interval '5 minutes'
      AND NOT EXISTS (
          SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
          WHERE retirement.device_id = device.id
      )
    FOR UPDATE;
    IF device_user IS DISTINCT FROM invitation.pending_user_id THEN
        RETURN NULL;
    END IF;
    old_actor := current_setting('app.current_user_id', true);
    PERFORM set_config('app.current_user_id', invitation.invited_by_id::text, true);
    SELECT (
        clinic_app.has_permission('staff.clinic', invitation.clinic_id, NULL)
        OR clinic_app.has_permission('staff.organization', invitation.clinic_id, NULL)
    ) AND (
        invitation.bundle NOT IN ('owner', 'org_admin')
        OR clinic_app.has_permission('staff.organization', invitation.clinic_id, NULL)
    ) INTO sender_authorized;
    PERFORM set_config('app.current_user_id', COALESCE(old_actor, ''), true);
    IF NOT sender_authorized THEN
        RETURN NULL;
    END IF;
    UPDATE clinic_app.identity_user
    SET username = requested_username, is_active = true
    WHERE id = invitation.pending_user_id AND NOT is_active;
    IF NOT FOUND THEN
        RETURN NULL;
    END IF;
    INSERT INTO clinic_app.identity_userclinicrole(
        id, organization_id, clinic_id, user_id, role, workflow_bundle_version,
        revoked_at
    ) VALUES (
        gen_random_uuid(), invitation.organization_id, invitation.clinic_id,
        invitation.pending_user_id, invitation.bundle, 1, NULL
    );
    UPDATE clinic_app.identity_staffinvitation
    SET state = 'accepted', accepted_at = statement_timestamp()
    WHERE id = invitation.id;
    RETURN invitation.pending_user_id;
END
$function$;

CREATE FUNCTION clinic_app.identity_invitation_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        IF OLD.state <> 'pending' AND NEW IS DISTINCT FROM OLD THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'invitation terminal history is immutable';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
           OR NEW.pending_user_id IS DISTINCT FROM OLD.pending_user_id
           OR NEW.invited_by_id IS DISTINCT FROM OLD.invited_by_id
           OR NEW.email_hash IS DISTINCT FROM OLD.email_hash
           OR NEW.email IS DISTINCT FROM OLD.email
           OR NEW.bundle IS DISTINCT FROM OLD.bundle
           OR NEW.token_hash IS DISTINCT FROM OLD.token_hash
           OR NEW.token_nonce IS DISTINCT FROM OLD.token_nonce
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.expires_at IS DISTINCT FROM OLD.expires_at
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'invitation identity is immutable';
        END IF;
    END IF;
    IF NEW.state = 'accepted' THEN
        IF current_user <> 'clinic_resolver'
           OR NEW.accepted_at IS NULL OR NEW.password_set_at IS NULL
           OR NOT EXISTS (
               SELECT 1 FROM clinic_app.otp_totp_totpdevice AS device
               JOIN clinic_app.identity_user AS account ON account.id = device.user_id
               WHERE device.id = NEW.device_id
                 AND device.user_id = NEW.pending_user_id
                 AND device.confirmed AND device.last_t >= 0 AND account.is_active
                 AND account.password <> '!'
           )
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'invitation requires password and confirmed TOTP';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

ALTER FUNCTION clinic_app.identity_invitation_user(uuid, uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_invitation_claim(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_invitation_begin(bytea, bytea, text, text)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_invitation_finish(bytea, text)
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_user(uuid, uuid)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_claim(bytea)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_begin(bytea, bytea, text, text)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_finish(bytea, text)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_invitation_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.identity_invitation_user(uuid, uuid)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_invitation_claim(bytea)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_invitation_begin(bytea, bytea, text, text)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_invitation_finish(bytea, text)
    TO clinic_app;
CREATE TRIGGER identity_invitation_boundary
    BEFORE INSERT OR UPDATE ON clinic_app.identity_staffinvitation
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_invitation_guard();
"""

"""Atomic one-use recovery and notification-clocked administrator reset."""

from typing import Final

RECOVERY_SQL: Final = """
CREATE FUNCTION clinic_app.identity_reset_target(
    requested_clinic uuid, requested_user uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT NULLIF(current_setting('app.current_principal', true), '') IS NULL
       AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
       AND (
           clinic_app.has_permission('staff.clinic', requested_clinic, NULL)
           OR clinic_app.has_permission('staff.organization', requested_clinic, NULL)
       )
       AND EXISTS (
           SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
           JOIN clinic_app.identity_user AS account ON account.id = membership.user_id
           WHERE membership.user_id = requested_user
             AND membership.clinic_id = requested_clinic
             AND membership.organization_id = NULLIF(
                 current_setting('app.current_tenant', true), ''
             )::uuid
             AND membership.revoked_at IS NULL AND account.is_active
       )
$function$;

CREATE FUNCTION clinic_app.identity_reset_recipient(requested_reset uuid)
RETURNS text LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT account.email FROM clinic_app.identity_authenticatorreset AS reset
    JOIN clinic_app.identity_user AS account ON account.id = reset.user_id
    WHERE reset.id = requested_reset
      AND clinic_app.identity_reset_target(reset.clinic_id, reset.user_id)
$function$;

CREATE FUNCTION clinic_app.identity_retire_authenticators(requested_user uuid)
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
    UPDATE clinic_app.identity_recoverybatch
    SET revoked_at = statement_timestamp()
    WHERE user_id = requested_user AND revoked_at IS NULL;
    UPDATE clinic_app.identity_sessionrecord
    SET revoked_at = statement_timestamp()
    WHERE user_id = requested_user AND revoked_at IS NULL;
END
$function$;

CREATE FUNCTION clinic_app.identity_recovery_consume(requested_hash bytea)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    requested_user uuid;
    selected_code uuid;
BEGIN
    requested_user := NULLIF(
        current_setting('app.current_user_id', true), ''
    )::uuid;
    IF requested_user IS NULL
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
    THEN
        RETURN false;
    END IF;
    PERFORM pg_advisory_xact_lock(hashtextextended(requested_user::text, 0));
    SELECT code.id INTO selected_code
    FROM clinic_app.identity_recoverycode AS code
    JOIN clinic_app.identity_recoverybatch AS batch ON batch.id = code.batch_id
    JOIN clinic_app.identity_user AS account ON account.id = batch.user_id
    JOIN clinic_app.otp_totp_totpdevice AS device ON device.id = batch.device_id
    WHERE batch.user_id = requested_user AND account.is_active
      AND batch.revoked_at IS NULL AND code.used_at IS NULL
      AND code.code_hash = requested_hash
      AND device.user_id = requested_user AND device.confirmed
      AND NOT EXISTS (
          SELECT 1 FROM clinic_app.identity_authenticatorretirement AS retirement
          WHERE retirement.device_id = device.id
      )
    FOR UPDATE OF code, batch;
    IF selected_code IS NULL THEN
        RETURN false;
    END IF;
    UPDATE clinic_app.identity_recoverycode
    SET used_at = statement_timestamp() WHERE id = selected_code;
    PERFORM clinic_app.identity_retire_authenticators(requested_user);
    RETURN true;
END
$function$;

CREATE FUNCTION clinic_app.identity_recovery_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'identity_recoverycode' THEN
        IF TG_OP = 'INSERT' THEN
            IF octet_length(NEW.code_hash) <> 32 OR NEW.used_at IS NOT NULL THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'recovery code requires an unused digest';
            END IF;
        ELSE
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.batch_id IS DISTINCT FROM OLD.batch_id
               OR NEW.code_hash IS DISTINCT FROM OLD.code_hash
               OR OLD.used_at IS NOT NULL
               OR NEW.used_at IS NULL OR current_user <> 'clinic_resolver'
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'recovery code consumption requires its boundary';
            END IF;
        END IF;
    ELSIF TG_TABLE_NAME = 'identity_recoverybatch' THEN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.user_id IS DISTINCT FROM OLD.user_id
               OR NEW.device_id IS DISTINCT FROM OLD.device_id
               OR NEW.static_device_id IS DISTINCT FROM OLD.static_device_id
               OR NEW.issued_at IS DISTINCT FROM OLD.issued_at
               OR OLD.revoked_at IS NOT NULL OR NEW.revoked_at IS NULL
            THEN
                RAISE EXCEPTION USING ERRCODE = '23514',
                    MESSAGE = 'recovery enrollment history is immutable';
            END IF;
        ELSIF NOT EXISTS (
            SELECT 1 FROM clinic_app.otp_totp_totpdevice AS device
            JOIN clinic_app.otp_static_staticdevice AS recovery
              ON recovery.id = NEW.static_device_id
            WHERE device.id = NEW.device_id AND device.user_id = NEW.user_id
              AND device.confirmed AND recovery.user_id = NEW.user_id
              AND recovery.confirmed
              AND NOT EXISTS (
                  SELECT 1 FROM clinic_app.identity_authenticatorretirement
                  WHERE device_id = device.id
              )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'recovery enrollment requires a confirmed authenticator';
        END IF;
    ELSE
        IF TG_OP = 'UPDATE' OR NOT EXISTS (
            SELECT 1 FROM clinic_app.otp_totp_totpdevice
            WHERE id = NEW.device_id AND user_id = NEW.user_id
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'authenticator retirement history is immutable';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION clinic_app.identity_reset_notification_clock()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF NEW.status = 'delivered' AND OLD.status <> 'delivered' THEN
        UPDATE clinic_app.identity_authenticatorreset
        SET notified_at = statement_timestamp(),
            available_at = statement_timestamp() + interval '24 hours'
        WHERE notification_operation_id = NEW.id AND notified_at IS NULL;
    END IF;
    RETURN NEW;
END
$function$;

CREATE FUNCTION clinic_app.identity_reset_execute(
    requested_reset uuid, requested_session bytea
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    reset clinic_app.identity_authenticatorreset%ROWTYPE;
BEGIN
    SELECT * INTO reset FROM clinic_app.identity_authenticatorreset
    WHERE id = requested_reset
      AND organization_id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
    FOR UPDATE;
    IF NOT FOUND OR reset.executed_at IS NOT NULL
       OR reset.notified_at IS NULL OR reset.available_at IS NULL
       OR reset.available_at <> reset.notified_at + interval '24 hours'
       OR reset.available_at > statement_timestamp()
       OR NOT clinic_app.identity_session_step_up(
           requested_session, reset.clinic_id
       )
       OR NOT EXISTS (
           SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
           JOIN clinic_app.identity_user AS account
             ON account.id = membership.user_id
           WHERE membership.user_id = reset.user_id
             AND membership.clinic_id = reset.clinic_id
             AND membership.organization_id = reset.organization_id
             AND membership.revoked_at IS NULL AND account.is_active
       )
       OR NOT EXISTS (
           SELECT 1 FROM clinic_app.comms_integrationoperation AS operation
           WHERE operation.id = reset.notification_operation_id
             AND operation.status = 'delivered'
       )
    THEN
        RETURN false;
    END IF;
    PERFORM clinic_app.identity_retire_authenticators(reset.user_id);
    UPDATE clinic_app.identity_authenticatorreset
    SET executed_at = statement_timestamp() WHERE id = reset.id;
    RETURN true;
END
$function$;

CREATE FUNCTION clinic_app.identity_reset_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.notified_at IS NOT NULL OR NEW.available_at IS NOT NULL
           OR NEW.executed_at IS NOT NULL
           OR NOT clinic_app.identity_reset_target(NEW.clinic_id, NEW.user_id)
           OR NEW.requested_by_id IS DISTINCT FROM NULLIF(
               current_setting('app.current_user_id', true), ''
           )::uuid
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reset starts without notification or execution';
        END IF;
    ELSE
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
           OR NEW.user_id IS DISTINCT FROM OLD.user_id
           OR NEW.requested_by_id IS DISTINCT FROM OLD.requested_by_id
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR (OLD.notification_operation_id IS NOT NULL
               AND NEW.notification_operation_id
                   IS DISTINCT FROM OLD.notification_operation_id)
           OR OLD.executed_at IS NOT NULL
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reset identity and execution history are immutable';
        END IF;
        IF (
            NEW.notified_at IS DISTINCT FROM OLD.notified_at
            OR NEW.available_at IS DISTINCT FROM OLD.available_at
            OR NEW.executed_at IS DISTINCT FROM OLD.executed_at
        ) AND current_user <> 'clinic_resolver' THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reset timing and execution require their boundaries';
        END IF;
        IF NEW.notified_at IS NOT NULL AND (
            NEW.available_at IS DISTINCT FROM NEW.notified_at + interval '24 hours'
            OR NOT EXISTS (
                SELECT 1 FROM clinic_app.comms_integrationoperation AS operation
                WHERE operation.id = NEW.notification_operation_id
                  AND operation.status = 'delivered'
            )
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'reset requires delivered notification and full delay';
        END IF;
    END IF;
    IF NEW.notification_operation_id IS NOT NULL AND NOT EXISTS (
        SELECT 1 FROM clinic_app.comms_integrationoperation AS operation
        WHERE operation.id = NEW.notification_operation_id
          AND operation.organization_id = NEW.organization_id
          AND operation.clinic_id = NEW.clinic_id
          AND operation.actor_id = NEW.requested_by_id
          AND operation.subject_id = NEW.id
          AND operation.subject_type = 'identity.authenticator_reset'
          AND operation.provider = 'identity-email-v1'
          AND operation.kind = 'communication' AND operation.channel = 'email'
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'reset requires its own scoped notification operation';
    END IF;
    RETURN NEW;
END
$function$;

ALTER FUNCTION clinic_app.identity_reset_target(uuid, uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_reset_recipient(uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_retire_authenticators(uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_recovery_consume(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_reset_notification_clock()
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_reset_execute(uuid, bytea)
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_retire_authenticators(uuid)
    FROM PUBLIC, clinic_app, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_reset_target(uuid, uuid)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_reset_recipient(uuid)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_recovery_consume(bytea)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_reset_notification_clock()
    FROM PUBLIC, clinic_app, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_reset_execute(uuid, bytea)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_recovery_guard() FROM PUBLIC;
REVOKE ALL ON FUNCTION clinic_app.identity_reset_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.identity_recovery_consume(bytea)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_reset_target(uuid, uuid)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_reset_recipient(uuid)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_reset_execute(uuid, bytea)
    TO clinic_app;
CREATE TRIGGER identity_recoverycode_boundary
    BEFORE INSERT OR UPDATE ON clinic_app.identity_recoverycode
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_recovery_guard();
CREATE TRIGGER identity_recoverybatch_boundary
    BEFORE INSERT OR UPDATE ON clinic_app.identity_recoverybatch
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_recovery_guard();
CREATE TRIGGER identity_retirement_boundary
    BEFORE INSERT OR UPDATE ON clinic_app.identity_authenticatorretirement
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_recovery_guard();
CREATE TRIGGER identity_reset_boundary
    BEFORE INSERT OR UPDATE ON clinic_app.identity_authenticatorreset
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_reset_guard();
CREATE TRIGGER identity_reset_delivered_clock
    AFTER UPDATE OF status ON clinic_app.comms_integrationoperation
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_reset_notification_clock();
"""

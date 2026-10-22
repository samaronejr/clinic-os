"""Exact tenant/owner policy and immutable public credential history."""

from typing import Final

PASSKEY_SQL: Final = """
REVOKE ALL ON clinic_app.identity_passkey FROM clinic_app, clinic_agent, PUBLIC;
GRANT SELECT, INSERT ON clinic_app.identity_passkey TO clinic_app;
GRANT UPDATE (removed_at) ON clinic_app.identity_passkey TO clinic_app;
GRANT SELECT ON clinic_app.identity_passkey TO clinic_resolver;
GRANT UPDATE (sign_count, last_used, removed_at)
    ON clinic_app.identity_passkey TO clinic_resolver;
ALTER TABLE clinic_app.identity_passkey ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_passkey FORCE ROW LEVEL SECURITY;
CREATE POLICY tenant_isolation ON clinic_app.identity_passkey
    USING (organization_id = NULLIF(
        current_setting('app.current_tenant', true), ''
    )::uuid)
    WITH CHECK (organization_id = NULLIF(
        current_setting('app.current_tenant', true), ''
    )::uuid);
CREATE POLICY passkey_owner_only ON clinic_app.identity_passkey
    AS RESTRICTIVE FOR ALL TO clinic_app
    USING (
        user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
        AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
        AND clinic_app.user_has_org(organization_id)
    )
    WITH CHECK (
        user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
        AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
        AND clinic_app.user_has_org(organization_id)
    );
ALTER TABLE clinic_app.identity_passkey
    ADD CONSTRAINT identity_passkey_clinic_org_fk
    FOREIGN KEY (organization_id, clinic_id)
    REFERENCES clinic_app.identity_clinic (organization_id, id)
    DEFERRABLE INITIALLY DEFERRED;

CREATE FUNCTION clinic_app.identity_passkey_guard()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
        RAISE EXCEPTION USING ERRCODE = '23514',
            MESSAGE = 'passkey history cannot be deleted';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.removed_at IS NOT NULL OR NEW.last_used IS NOT NULL
           OR octet_length(NEW.credential_id) NOT BETWEEN 1 AND 1023
           OR octet_length(NEW.public_key) NOT BETWEEN 1 AND 8192
           OR jsonb_typeof(NEW.transports) <> 'array'
           OR jsonb_array_length(NEW.transports) > 8
           OR NEW.transports - ARRAY[
               'usb', 'nfc', 'ble', 'smart-card', 'internal', 'cable', 'hybrid'
           ] <> '[]'::jsonb
           OR NOT EXISTS (
               SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
               WHERE membership.user_id = NEW.user_id
                 AND membership.clinic_id = NEW.clinic_id
                 AND membership.organization_id = NEW.organization_id
                 AND membership.revoked_at IS NULL
           )
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'passkey registration requires its own current scope';
        END IF;
    ELSE
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.organization_id IS DISTINCT FROM OLD.organization_id
           OR NEW.user_id IS DISTINCT FROM OLD.user_id
           OR NEW.clinic_id IS DISTINCT FROM OLD.clinic_id
           OR NEW.credential_id IS DISTINCT FROM OLD.credential_id
           OR NEW.public_key IS DISTINCT FROM OLD.public_key
           OR NEW.transports IS DISTINCT FROM OLD.transports
           OR NEW.created_at IS DISTINCT FROM OLD.created_at
           OR NEW.nickname IS DISTINCT FROM OLD.nickname
           OR OLD.removed_at IS NOT NULL
        THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'passkey identity and removal are immutable';
        END IF;
        IF (
            NEW.sign_count IS DISTINCT FROM OLD.sign_count
            OR NEW.last_used IS DISTINCT FROM OLD.last_used
        ) AND (
            current_user <> 'clinic_resolver'
            OR NEW.removed_at IS NOT NULL
            OR (NEW.sign_count <= OLD.sign_count
                AND NOT (NEW.sign_count = 0 AND OLD.sign_count = 0))
            OR NEW.last_used IS NULL
            OR (OLD.last_used IS NOT NULL AND NEW.last_used < OLD.last_used)
        ) THEN
            RAISE EXCEPTION USING ERRCODE = '23514',
                MESSAGE = 'passkey use requires its verification boundary';
        END IF;
    END IF;
    RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.identity_passkey_guard() FROM PUBLIC;
CREATE TRIGGER identity_passkey_history
    BEFORE INSERT OR UPDATE OR DELETE ON clinic_app.identity_passkey
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_passkey_guard();
CREATE TRIGGER identity_passkey_no_truncate
    BEFORE TRUNCATE ON clinic_app.identity_passkey
    FOR EACH STATEMENT EXECUTE FUNCTION clinic_app.identity_passkey_guard();
"""

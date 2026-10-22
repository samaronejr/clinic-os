"""Fixed creation-only signup boundary and protected pending-tenant posture."""

from typing import Final

SIGNUP_ARGUMENTS: Final = """
    uuid, uuid, uuid, uuid, jsonb, text, bytea, text
"""

SIGNUP_SQL: Final = """
CREATE POLICY active_organization_runtime ON clinic_app.identity_organization
    AS RESTRICTIVE FOR SELECT TO clinic_app USING (status = 'active');
ALTER TABLE clinic_app.identity_signupregistration ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.identity_signupregistration FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.identity_signupregistration
    FROM PUBLIC, clinic_app, clinic_agent, clinic_resolver;
CREATE POLICY signup_owner ON clinic_app.identity_signupregistration
    FOR ALL TO clinic_owner USING (true) WITH CHECK (true);
ALTER TABLE clinic_app.identity_signupregistration
    ADD CONSTRAINT identity_signup_fingerprint_length
        CHECK (octet_length(payload_fingerprint) = 32);

CREATE FUNCTION clinic_app.identity_signup_immutable()
RETURNS trigger LANGUAGE plpgsql
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    RAISE EXCEPTION 'signup capture history is immutable' USING ERRCODE = '23514';
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.identity_signup_immutable() FROM PUBLIC;
CREATE TRIGGER signup_immutable
    BEFORE UPDATE OR DELETE ON clinic_app.identity_signupregistration
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_signup_immutable();

SET LOCAL ROLE clinic_resolver;
CREATE FUNCTION clinic_app.identity_pending_authority_guard()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
BEGIN
    IF TG_TABLE_NAME = 'identity_userclinicrole' THEN
        IF NOT EXISTS (
            SELECT 1 FROM clinic_app.identity_organization AS organization
            WHERE organization.id = NEW.organization_id
              AND organization.status = 'active'
        ) THEN
            RAISE EXCEPTION 'organization is not active' USING ERRCODE = '23514';
        END IF;
    ELSIF NEW.status = 'pending_activation' AND EXISTS (
        SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
        WHERE membership.organization_id = NEW.id
    ) THEN
        RAISE EXCEPTION 'pending organization cannot hold authority'
            USING ERRCODE = '23514';
    END IF;
    RETURN NEW;
END
$function$;
REVOKE ALL ON FUNCTION clinic_app.identity_pending_authority_guard() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION clinic_app.identity_pending_authority_guard()
    TO clinic_owner;
RESET ROLE;
CREATE TRIGGER pending_membership_guard
    BEFORE INSERT OR UPDATE ON clinic_app.identity_userclinicrole
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_pending_authority_guard();
CREATE TRIGGER pending_organization_guard
    BEFORE UPDATE OF status ON clinic_app.identity_organization
    FOR EACH ROW EXECUTE FUNCTION clinic_app.identity_pending_authority_guard();
REVOKE EXECUTE ON FUNCTION clinic_app.identity_pending_authority_guard()
    FROM clinic_owner;

CREATE FUNCTION clinic_app.identity_signup_register(
    registration_id uuid,
    requested_organization_id uuid,
    requested_admin_id uuid,
    request_key uuid,
    capture jsonb,
    encoded_password text,
    fingerprint bytea,
    kek text
)
RETURNS TABLE (organization_id uuid, first_admin_id uuid, created boolean)
LANGUAGE plpgsql VOLATILE PARALLEL UNSAFE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    saved_tenant text := current_setting('app.current_tenant', true);
    saved_actor text := current_setting('app.current_user_id', true);
    existing clinic_app.identity_signupregistration%ROWTYPE;
    item record;
    cnpj text;
    check_sum integer;
    check_digit integer;
    digit_index integer;
    check_index integer;
    weights integer[];
    occurred_at_utc timestamptz := statement_timestamp();
    canonical_audit text;
    content_hash bytea;
BEGIN
    IF NULLIF(saved_tenant, '') IS NOT NULL
       OR NULLIF(saved_actor, '') IS NOT NULL
       OR NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
       OR registration_id IS NULL OR requested_organization_id IS NULL
       OR requested_admin_id IS NULL OR request_key IS NULL
       OR requested_organization_id = '00000000-0000-0000-0000-000000000000'::uuid
       OR capture IS NULL OR jsonb_typeof(capture) <> 'object'
       OR fingerprint IS NULL OR octet_length(fingerprint) <> 32
       OR encoded_password IS NULL OR length(encoded_password) NOT BETWEEN 1 AND 128
       OR encoded_password !~ '^[a-zA-Z0-9_]+\\$'
       OR kek IS NULL OR kek !~ '^[a-fA-F0-9]{64}$'
    THEN
        RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
    END IF;
    IF (
        SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(capture) AS key
    ) IS DISTINCT FROM ARRAY[
        'cnpj', 'email', 'intended_bundle', 'legal_responsible',
        'organization_name', 'professional_registration', 'username'
    ]::text[] THEN
        RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
    END IF;
    FOR item IN SELECT key, value FROM jsonb_each(capture) LOOP
        IF jsonb_typeof(item.value) <> 'string'
           OR length(item.value #>> '{}') > (CASE item.key
                WHEN 'professional_registration' THEN 512
                WHEN 'username' THEN 150
                WHEN 'email' THEN 254
                WHEN 'cnpj' THEN 14
                ELSE 255
              END)
           OR (
                item.key <> 'professional_registration'
                AND length(btrim(item.value #>> '{}')) = 0
           )
        THEN
            RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
        END IF;
    END LOOP;
    IF capture ->> 'intended_bundle' <> 'org_admin' THEN
        RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
    END IF;
    cnpj := capture ->> 'cnpj';
    IF cnpj !~ '^[0-9]{14}$'
       OR cnpj = repeat(substr(cnpj, 1, 1), 14) THEN
        RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
    END IF;
    FOR check_index IN 1..2 LOOP
        weights := CASE check_index
            WHEN 1 THEN ARRAY[5,4,3,2,9,8,7,6,5,4,3,2]
            ELSE ARRAY[6,5,4,3,2,9,8,7,6,5,4,3,2]
        END;
        check_sum := 0;
        FOR digit_index IN 1..array_length(weights, 1) LOOP
            check_sum := check_sum
                + substr(cnpj, digit_index, 1)::integer * weights[digit_index];
        END LOOP;
        check_digit := CASE check_sum % 11
            WHEN 0 THEN 0 WHEN 1 THEN 0 ELSE 11 - check_sum % 11
        END;
        IF substr(cnpj, 12 + check_index, 1)::integer <> check_digit THEN
            RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
        END IF;
    END LOOP;

    PERFORM pg_advisory_xact_lock(
        hashtextextended('identity-signup:' || request_key::text, 0)
    );
    SELECT registration.* INTO existing
    FROM clinic_app.identity_signupregistration AS registration
    WHERE registration.idempotency_key = request_key;
    IF FOUND THEN
        IF existing.payload_fingerprint IS DISTINCT FROM fingerprint THEN
            RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
        END IF;
        PERFORM set_config('app.current_tenant', existing.organization_id::text, true);
        IF convert_from(
                clinic_app.tenant_decrypt(
                    kek, 'identity.signup.capture', existing.payload
                ),
                'UTF8'
           )::jsonb IS DISTINCT FROM capture THEN
            RAISE EXCEPTION 'signup refused' USING ERRCODE = '42501';
        END IF;
        PERFORM set_config('app.current_tenant', COALESCE(saved_tenant, ''), true);
        RETURN QUERY SELECT existing.organization_id, existing.first_admin_id, false;
        RETURN;
    END IF;

    PERFORM set_config('app.current_tenant', requested_organization_id::text, true);
    INSERT INTO clinic_app.identity_organization (id, name, cnpj, status)
        VALUES (requested_organization_id, '', '', 'pending_activation');
    INSERT INTO clinic_app.identity_user (
        id, password, last_login, is_superuser, username, first_name, last_name,
        email, is_staff, is_active, date_joined
    ) VALUES (
        requested_admin_id, encoded_password, NULL, false,
        'pending-' || requested_admin_id::text, '', '', '', false, false,
        statement_timestamp()
    );
    canonical_audit :=
        '{"actor":{"user_id":null},"affected_record_id":"'
        || registration_id::text
        || '","affected_record_type":"identity.signup_registration",'
        || '"component":{"identifier":"clinic-os-web","ip":null},'
        || '"event_type":"identity.signup.requested","occurred_at_utc":"'
        || to_char(occurred_at_utc AT TIME ZONE 'UTC',
            'YYYY-MM-DD"T"HH24:MI:SS.US"Z"')
        || '","organization_id":"00000000-0000-0000-0000-000000000000",'
        || '"payload":{"clinic_id":"00000000-0000-0000-0000-000000000000",'
        || '"object_verb":"requested"},"v":"clinic-audit-v1"}';
    content_hash := clinic_app.digest(
        convert_to('clinic-audit-v1', 'UTF8') || decode('00', 'hex')
        || convert_to(canonical_audit, 'UTF8'),
        'sha256'
    );
    PERFORM clinic_app.tenant_dek_issue(kek);
    INSERT INTO clinic_app.identity_signupregistration (
        id, organization_id, first_admin_id, idempotency_key, payload,
        payload_fingerprint, created_at
    ) VALUES (
        registration_id, requested_organization_id, requested_admin_id, request_key,
        clinic_app.tenant_encrypt(
            kek, 'identity.signup.capture', convert_to(capture::text, 'UTF8')
        ),
        fingerprint, statement_timestamp()
    );
    PERFORM clinic_app.audit_append_system_unchecked_v2(
        'identity.signup.requested', 'clinic-os-web', NULL,
        'identity.signup_registration', registration_id::text, occurred_at_utc,
        jsonb_build_object(
            'clinic_id', '00000000-0000-0000-0000-000000000000',
            'object_verb', 'requested'
        ),
        content_hash
    );
    PERFORM set_config('app.current_tenant', COALESCE(saved_tenant, ''), true);
    PERFORM set_config('app.current_user_id', COALESCE(saved_actor, ''), true);
    RETURN QUERY SELECT requested_organization_id, requested_admin_id, true;
END
$function$;
ALTER FUNCTION clinic_app.identity_signup_register(__SIGNUP_ARGUMENTS__)
    OWNER TO clinic_owner;
REVOKE ALL ON FUNCTION clinic_app.identity_signup_register(__SIGNUP_ARGUMENTS__)
    FROM PUBLIC, clinic_resolver, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_signup_register(__SIGNUP_ARGUMENTS__)
    TO clinic_app;
""".replace("__SIGNUP_ARGUMENTS__", SIGNUP_ARGUMENTS)

REMOVE_SIGNUP_SQL: Final = """
DROP FUNCTION clinic_app.identity_signup_register(__SIGNUP_ARGUMENTS__);
DROP POLICY active_organization_runtime ON clinic_app.identity_organization;
DROP TRIGGER signup_immutable ON clinic_app.identity_signupregistration;
DROP FUNCTION clinic_app.identity_signup_immutable();
DROP TRIGGER pending_membership_guard ON clinic_app.identity_userclinicrole;
DROP TRIGGER pending_organization_guard ON clinic_app.identity_organization;
SET LOCAL ROLE clinic_resolver;
DROP FUNCTION clinic_app.identity_pending_authority_guard();
RESET ROLE;
DROP POLICY signup_owner ON clinic_app.identity_signupregistration;
""".replace("__SIGNUP_ARGUMENTS__", SIGNUP_ARGUMENTS)

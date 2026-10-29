"""Fresh-factor controls and explicit organization-wide passkey substitution."""

from typing import Final

POLICY_SQL: Final = """
CREATE FUNCTION clinic_app.identity_session_authenticator_verified(requested_hash bytea)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT clinic_app.identity_session_verified(requested_hash)
        OR clinic_app.identity_passkey_session_verified(
            requested_hash,
            (SELECT verification_passkey_id
             FROM clinic_app.identity_sessionrecord
             WHERE session_key_hash = requested_hash
               AND user_id = NULLIF(
                   current_setting('app.current_user_id', true), ''
               )::uuid),
            300
        )
$function$;

CREATE FUNCTION clinic_app.identity_passkey_registration_allowed(requested_hash bytea)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT clinic_app.identity_session_authenticator_verified(requested_hash)
        OR EXISTS (
            SELECT 1 FROM clinic_app.identity_sessionrecord AS session
            JOIN clinic_app.identity_organization AS organization
              ON organization.id = NULLIF(
                  current_setting('app.current_tenant', true), ''
              )::uuid
            WHERE session.session_key_hash = requested_hash
              AND session.user_id = NULLIF(
                  current_setting('app.current_user_id', true), ''
              )::uuid
              AND session.revoked_at IS NULL
              AND session.expires_at > statement_timestamp()
              AND session.created_at <= statement_timestamp()
              AND session.created_at >=
                  statement_timestamp() - interval '5 minutes'
              AND clinic_app.user_has_org(organization.id)
              AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
              AND NULLIF(
                  current_setting('app.current_patient_session', true), ''
              ) IS NULL
              AND (
                  organization.passkey_step_up_enabled
                  OR NOT EXISTS (
                      SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
                      WHERE membership.user_id = session.user_id
                        AND membership.organization_id = organization.id
                        AND membership.revoked_at IS NULL
                        AND membership.role IN (
                            'owner', 'physician', 'clinic_admin',
                            'clinic_manager', 'org_admin'
                        )
                  )
              )
        )
$function$;

SET LOCAL ROLE clinic_resolver;
CREATE OR REPLACE FUNCTION clinic_app.identity_session_step_up(
    requested_hash bytea, requested_clinic uuid
)
RETURNS boolean LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT clinic_app.identity_session_authenticator_verified(requested_hash) AND (
        clinic_app.has_permission('staff.clinic', requested_clinic, NULL)
        OR clinic_app.has_permission('staff.organization', requested_clinic, NULL)
    )
$function$;
RESET ROLE;

GRANT UPDATE (passkey_step_up_enabled)
    ON clinic_app.identity_organization TO clinic_resolver;
CREATE FUNCTION clinic_app.identity_passkey_policy_set(
    requested_clinic uuid, requested_enabled boolean, requested_session bytea
)
RETURNS integer LANGUAGE plpgsql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    target clinic_app.identity_organization%ROWTYPE;
BEGIN
    SELECT organization.* INTO target
    FROM clinic_app.identity_organization AS organization
    JOIN clinic_app.identity_clinic AS clinic
      ON clinic.organization_id = organization.id
    WHERE clinic.id = requested_clinic
      AND organization.id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
    FOR UPDATE OF organization;
    IF NOT FOUND OR requested_enabled IS NULL OR target.status <> 'active'
       OR NOT clinic_app.identity_session_authenticator_verified(requested_session)
       OR NOT EXISTS (
           SELECT 1 FROM clinic_app.identity_clinic WHERE organization_id = target.id
       )
       OR EXISTS (
           SELECT 1 FROM clinic_app.identity_clinic AS clinic
           WHERE clinic.organization_id = target.id
             AND NOT clinic_app.has_permission('staff.organization', clinic.id, NULL)
       )
    THEN
        RETURN -1;
    END IF;
    IF target.passkey_step_up_enabled = requested_enabled THEN
        RETURN 0;
    END IF;
    UPDATE clinic_app.identity_organization
    SET passkey_step_up_enabled = requested_enabled
    WHERE id = target.id;
    RETURN 1;
END
$function$;
ALTER FUNCTION clinic_app.identity_session_authenticator_verified(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_passkey_registration_allowed(bytea)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_passkey_policy_set(uuid, boolean, bytea)
    OWNER TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_session_authenticator_verified(bytea),
    clinic_app.identity_passkey_registration_allowed(bytea),
    clinic_app.identity_passkey_policy_set(uuid, boolean, bytea)
    FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_session_authenticator_verified(bytea),
    clinic_app.identity_passkey_registration_allowed(bytea),
    clinic_app.identity_passkey_policy_set(uuid, boolean, bytea)
    TO clinic_app;
RESET ROLE;
"""

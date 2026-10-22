"""Scoped, final-owner-safe offboarding retaining accounts and role history."""

from typing import Final

OFFBOARDING_SQL: Final = """
CREATE FUNCTION clinic_app.identity_offboarding_clinics(
    requested_user uuid, requested_clinic uuid
)
RETURNS uuid[] LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    current_organization uuid;
    memberships uuid[];
BEGIN
    current_organization := NULLIF(
        current_setting('app.current_tenant', true), ''
    )::uuid;
    IF NULLIF(current_setting('app.current_principal', true), '') IS NOT NULL
       OR NULLIF(current_setting('app.current_patient_session', true), '') IS NOT NULL
       OR requested_user = NULLIF(
        current_setting('app.current_user_id', true), ''
    )::uuid OR NOT (
        clinic_app.has_permission('staff.clinic', requested_clinic, NULL)
        OR clinic_app.has_permission('staff.organization', requested_clinic, NULL)
    ) OR NOT EXISTS (
        SELECT 1 FROM clinic_app.identity_userclinicrole
        WHERE user_id = requested_user AND clinic_id = requested_clinic
          AND organization_id = current_organization
    ) OR EXISTS (
        SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
        WHERE membership.user_id = requested_user AND membership.revoked_at IS NULL
          AND (
              membership.organization_id <> current_organization
              OR NOT (
                  clinic_app.has_permission('staff.clinic', membership.clinic_id, NULL)
                  OR clinic_app.has_permission(
                      'staff.organization', membership.clinic_id, NULL
                  )
              )
              OR (
                  membership.role IN ('owner', 'org_admin')
                  AND NOT clinic_app.has_permission(
                      'staff.organization', membership.clinic_id, NULL
                  )
              )
          )
    ) THEN
        RETURN NULL;
    END IF;
    SELECT array_agg(DISTINCT clinic_id ORDER BY clinic_id) INTO memberships
    FROM clinic_app.identity_userclinicrole
    WHERE user_id = requested_user AND revoked_at IS NULL;
    RETURN COALESCE(memberships, ARRAY[requested_clinic]);
END
$function$;

CREATE FUNCTION clinic_app.identity_offboarding_execute(
    requested_user uuid, requested_clinic uuid
)
RETURNS boolean LANGUAGE plpgsql SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
DECLARE
    current_organization uuid;
    account_active boolean;
BEGIN
    IF clinic_app.identity_offboarding_clinics(
        requested_user, requested_clinic
    ) IS NULL THEN
        RETURN false;
    END IF;
    current_organization := NULLIF(
        current_setting('app.current_tenant', true), ''
    )::uuid;
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'clinic-lock-v1:identity-owner:' || current_organization::text, 0
    ));
    PERFORM pg_advisory_xact_lock(hashtextextended(
        'clinic-lock-v1:user:' || requested_user::text, 0
    ));
    SELECT is_active INTO account_active FROM clinic_app.identity_user
    WHERE id = requested_user FOR UPDATE;
    IF NOT FOUND THEN
        RETURN false;
    END IF;
    IF NOT account_active THEN
        RETURN true;
    END IF;
    IF EXISTS (
        SELECT 1 FROM clinic_app.identity_userclinicrole AS membership
        WHERE membership.user_id = requested_user AND membership.role = 'owner'
          AND membership.revoked_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM clinic_app.identity_userclinicrole AS other
              JOIN clinic_app.identity_user AS account ON account.id = other.user_id
              WHERE other.organization_id = membership.organization_id
                AND other.role = 'owner' AND other.revoked_at IS NULL
                AND other.user_id <> requested_user AND account.is_active
          )
    ) OR EXISTS (
        SELECT 1 FROM clinic_app.scheduling_appointment
        WHERE practitioner_id = requested_user AND status = 'scheduled'
          AND end_at > statement_timestamp()
    ) OR EXISTS (
        SELECT 1 FROM clinic_app.scheduling_availabilityblock
        WHERE practitioner_id = requested_user AND retired_at IS NULL
          AND end_at > statement_timestamp()
    ) THEN
        RETURN false;
    END IF;
    UPDATE clinic_app.identity_userclinicrole
    SET revoked_at = statement_timestamp()
    WHERE user_id = requested_user AND revoked_at IS NULL;
    PERFORM clinic_app.identity_retire_authenticators(requested_user);
    UPDATE clinic_app.identity_user SET is_active = false WHERE id = requested_user;
    RETURN true;
END
$function$;

ALTER FUNCTION clinic_app.identity_offboarding_clinics(uuid, uuid)
    OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_offboarding_execute(uuid, uuid)
    OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_offboarding_clinics(uuid, uuid)
    FROM PUBLIC, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_offboarding_execute(uuid, uuid)
    FROM PUBLIC, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_offboarding_clinics(uuid, uuid)
    TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_offboarding_execute(uuid, uuid)
    TO clinic_app;
"""

"""Exact-user authenticator policies and current-clinic staff directory."""

from typing import Final

FORWARD_SQL: Final = """
CREATE POLICY invitation_owner ON clinic_app.otp_totp_totpdevice
    FOR ALL TO clinic_owner
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (
        user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
    );

ALTER TABLE clinic_app.otp_static_staticdevice ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.otp_static_staticdevice FORCE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.otp_static_statictoken ENABLE ROW LEVEL SECURITY;
ALTER TABLE clinic_app.otp_static_statictoken FORCE ROW LEVEL SECURITY;
REVOKE ALL ON clinic_app.otp_static_staticdevice,
    clinic_app.otp_static_statictoken FROM PUBLIC, clinic_app, clinic_agent;
GRANT SELECT, INSERT ON clinic_app.otp_static_staticdevice TO clinic_app;
CREATE POLICY own_user ON clinic_app.otp_static_staticdevice
    FOR ALL TO clinic_app
    USING (user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid)
    WITH CHECK (
        user_id = NULLIF(current_setting('app.current_user_id', true), '')::uuid
        AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
        AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
    );
CREATE POLICY owner ON clinic_app.otp_static_staticdevice
    FOR ALL TO clinic_owner USING (true) WITH CHECK (true);
CREATE POLICY owner ON clinic_app.otp_static_statictoken
    FOR ALL TO clinic_owner USING (true) WITH CHECK (true);

CREATE FUNCTION clinic_app.identity_staff_directory(requested_clinic uuid)
RETURNS TABLE(user_id uuid, display_label text, bundle text)
LANGUAGE sql STABLE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT account.id, account.username::text, membership.role::text
    FROM clinic_app.identity_userclinicrole AS membership
    JOIN clinic_app.identity_user AS account ON account.id = membership.user_id
    WHERE membership.clinic_id = requested_clinic
      AND membership.organization_id = NULLIF(
          current_setting('app.current_tenant', true), ''
      )::uuid
      AND membership.revoked_at IS NULL AND account.is_active
      AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
      AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
      AND (
          clinic_app.has_permission('staff.clinic', requested_clinic, NULL)
          OR clinic_app.has_permission('staff.organization', requested_clinic, NULL)
      )
    ORDER BY account.username, account.id, membership.role
$function$;
ALTER FUNCTION clinic_app.identity_staff_directory(uuid) OWNER TO clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_staff_directory(uuid)
    FROM PUBLIC, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_staff_directory(uuid) TO clinic_app;
"""

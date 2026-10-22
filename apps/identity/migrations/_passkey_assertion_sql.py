"""Sessionless credential lookup and monotonic, own-scope use metadata."""

from typing import Final

ASSERTION_SQL: Final = """
CREATE FUNCTION clinic_app.identity_passkey_lookup(requested_credential bytea)
RETURNS TABLE(
    passkey_id uuid, user_id uuid, organization_id uuid, clinic_id uuid,
    username text, encoded_password text
)
LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    SELECT DISTINCT ON (credential.id)
        credential.id, credential.user_id, credential.organization_id,
        membership.clinic_id, account.username::text, account.password::text
    FROM clinic_app.identity_passkey AS credential
    JOIN clinic_app.identity_user AS account
      ON account.id = credential.user_id AND account.is_active
    JOIN clinic_app.identity_organization AS organization
      ON organization.id = credential.organization_id
      AND organization.status = 'active'
    JOIN clinic_app.identity_userclinicrole AS membership
      ON membership.user_id = credential.user_id
      AND membership.organization_id = credential.organization_id
      AND membership.revoked_at IS NULL
    WHERE credential.credential_id = requested_credential
      AND credential.removed_at IS NULL
      AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
      AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
    ORDER BY credential.id, membership.clinic_id
$function$;

CREATE FUNCTION clinic_app.identity_passkey_use(
    requested_passkey uuid, requested_counter bigint
)
RETURNS boolean LANGUAGE sql VOLATILE SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $function$
    WITH used AS (
        UPDATE clinic_app.identity_passkey AS credential
        SET sign_count = requested_counter, last_used = statement_timestamp()
        WHERE credential.id = requested_passkey
          AND credential.user_id = NULLIF(
              current_setting('app.current_user_id', true), ''
          )::uuid
          AND credential.organization_id = NULLIF(
              current_setting('app.current_tenant', true), ''
          )::uuid
          AND credential.removed_at IS NULL
          AND requested_counter BETWEEN 0 AND 4294967295
          AND (requested_counter > credential.sign_count
               OR (requested_counter = 0 AND credential.sign_count = 0))
          AND NULLIF(current_setting('app.current_principal', true), '') IS NULL
          AND NULLIF(current_setting('app.current_patient_session', true), '') IS NULL
          AND clinic_app.user_has_org(credential.organization_id)
        RETURNING credential.id
    )
    SELECT EXISTS (SELECT 1 FROM used)
$function$;
ALTER FUNCTION clinic_app.identity_passkey_lookup(bytea) OWNER TO clinic_resolver;
ALTER FUNCTION clinic_app.identity_passkey_use(uuid, bigint) OWNER TO clinic_resolver;
SET LOCAL ROLE clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.identity_passkey_lookup(bytea)
    FROM PUBLIC, clinic_app, clinic_agent;
REVOKE ALL ON FUNCTION clinic_app.identity_passkey_use(uuid, bigint)
    FROM PUBLIC, clinic_app, clinic_agent;
GRANT EXECUTE ON FUNCTION clinic_app.identity_passkey_lookup(bytea) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.identity_passkey_use(uuid, bigint) TO clinic_app;
RESET ROLE;
"""

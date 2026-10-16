"""Keep demographic registry search and current manager membership together."""

from typing import Final

REGISTRY_SQL: Final = """SET LOCAL ROLE clinic_owner;

CREATE OR REPLACE FUNCTION clinic_app.patient_registry_count(
    kek pg_catalog.text,
    requested_clinic pg_catalog.uuid,
    normalized_query pg_catalog.text,
    requested_birth pg_catalog.date
)
RETURNS pg_catalog.int8
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
DECLARE
    total pg_catalog.int8;
BEGIN

    IF NOT EXISTS (
        SELECT 1
          FROM clinic_app.identity_userclinicrole AS role_row
          JOIN clinic_app.identity_user AS user_row
            ON user_row.id = role_row.user_id AND user_row.is_active
         WHERE role_row.user_id = NULLIF(
             pg_catalog.current_setting('app.current_user_id', true), ''
         )::pg_catalog.uuid
           AND role_row.organization_id = NULLIF(
               pg_catalog.current_setting('app.current_tenant', true), ''
           )::pg_catalog.uuid
           AND role_row.revoked_at IS NULL
           AND role_row.clinic_id = requested_clinic
           AND role_row.role IN ('owner', 'clinic_admin', 'receptionist')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'clinic manager authority is required';
    END IF;

    SELECT pg_catalog.count(*) INTO total
      FROM (

        SELECT enrollment_row.id AS enrollment_id,
               patient_row.id AS patient_id,
               pg_catalog.convert_from(
                   clinic_app.protected_decrypt(
                       kek, 'intake.patient.full_name', patient_row.full_name
                   ),
                   'UTF8'
               ) COLLATE pg_catalog."default" AS decrypted_name,
               CASE WHEN patient_row.birth_date IS NULL THEN NULL
                    ELSE pg_catalog.convert_from(
                        clinic_app.protected_decrypt(
                            kek,
                            'intake.patient.birth_date',
                            patient_row.birth_date
                        ),
                        'UTF8'
                    )::pg_catalog.date
               END AS decrypted_birth,
               CASE WHEN demographics_row.social_name IS NULL THEN NULL
                    ELSE pg_catalog.convert_from(
                        clinic_app.protected_decrypt(
                            kek,
                            'intake.patientdemographics.social_name',
                            demographics_row.social_name
                        ),
                        'UTF8'
                    )
               END COLLATE pg_catalog."default" AS decrypted_social_name
          FROM clinic_app.intake_patientclinicenrollment AS enrollment_row
          JOIN clinic_app.intake_patient AS patient_row
            ON patient_row.id = enrollment_row.patient_id
           AND patient_row.organization_id = enrollment_row.organization_id
          LEFT JOIN clinic_app.intake_patientdemographics AS demographics_row
            ON demographics_row.organization_id = enrollment_row.organization_id
           AND demographics_row.patient_id = enrollment_row.patient_id
           AND demographics_row.version = (
               SELECT pg_catalog.max(latest_row.version)
                 FROM clinic_app.intake_patientdemographics AS latest_row
                WHERE latest_row.organization_id = enrollment_row.organization_id
                  AND latest_row.patient_id = enrollment_row.patient_id
           )
         WHERE enrollment_row.clinic_id = requested_clinic
           AND enrollment_row.organization_id = NULLIF(
               pg_catalog.current_setting('app.current_tenant', true), ''
           )::pg_catalog.uuid

      ) AS decrypted
     WHERE\x20
        (pg_catalog.strpos(
            clinic_app.unaccent(
                pg_catalog.lower(decrypted.decrypted_name)
            ),
            clinic_app.unaccent(pg_catalog.lower(normalized_query))
        ) > 0
        OR pg_catalog.strpos(
            clinic_app.unaccent(
                pg_catalog.lower(
                    COALESCE(decrypted.decrypted_social_name, '')
                )
            ),
            clinic_app.unaccent(pg_catalog.lower(normalized_query))
        ) > 0)

       AND (requested_birth IS NULL
            OR decrypted.decrypted_birth = requested_birth);
    RETURN total;
END
$function$;

DROP FUNCTION IF EXISTS clinic_app.patient_registry_page(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date,
    pg_catalog.int4, pg_catalog.int4
);
CREATE FUNCTION clinic_app.patient_registry_page(
    kek pg_catalog.text,
    requested_clinic pg_catalog.uuid,
    normalized_query pg_catalog.text,
    requested_birth pg_catalog.date,
    page_offset pg_catalog.int4,
    page_limit pg_catalog.int4
)
RETURNS TABLE(
    enrollment_id pg_catalog.uuid,
    full_name pg_catalog.text,
    birth_date pg_catalog.date,
    social_name pg_catalog.text
)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, clinic_app, pg_temp
AS $function$
BEGIN

    IF NOT EXISTS (
        SELECT 1
          FROM clinic_app.identity_userclinicrole AS role_row
          JOIN clinic_app.identity_user AS user_row
            ON user_row.id = role_row.user_id AND user_row.is_active
         WHERE role_row.user_id = NULLIF(
             pg_catalog.current_setting('app.current_user_id', true), ''
         )::pg_catalog.uuid
           AND role_row.organization_id = NULLIF(
               pg_catalog.current_setting('app.current_tenant', true), ''
           )::pg_catalog.uuid
           AND role_row.revoked_at IS NULL
           AND role_row.clinic_id = requested_clinic
           AND role_row.role IN ('owner', 'clinic_admin', 'receptionist')
    ) THEN
        RAISE EXCEPTION USING ERRCODE = '42501',
            MESSAGE = 'clinic manager authority is required';
    END IF;

    RETURN QUERY
    SELECT decrypted.enrollment_id, decrypted.decrypted_name,
           decrypted.decrypted_birth, decrypted.decrypted_social_name
      FROM (

        SELECT enrollment_row.id AS enrollment_id,
               patient_row.id AS patient_id,
               pg_catalog.convert_from(
                   clinic_app.protected_decrypt(
                       kek, 'intake.patient.full_name', patient_row.full_name
                   ),
                   'UTF8'
               ) COLLATE pg_catalog."default" AS decrypted_name,
               CASE WHEN patient_row.birth_date IS NULL THEN NULL
                    ELSE pg_catalog.convert_from(
                        clinic_app.protected_decrypt(
                            kek,
                            'intake.patient.birth_date',
                            patient_row.birth_date
                        ),
                        'UTF8'
                    )::pg_catalog.date
               END AS decrypted_birth,
               CASE WHEN demographics_row.social_name IS NULL THEN NULL
                    ELSE pg_catalog.convert_from(
                        clinic_app.protected_decrypt(
                            kek,
                            'intake.patientdemographics.social_name',
                            demographics_row.social_name
                        ),
                        'UTF8'
                    )
               END COLLATE pg_catalog."default" AS decrypted_social_name
          FROM clinic_app.intake_patientclinicenrollment AS enrollment_row
          JOIN clinic_app.intake_patient AS patient_row
            ON patient_row.id = enrollment_row.patient_id
           AND patient_row.organization_id = enrollment_row.organization_id
          LEFT JOIN clinic_app.intake_patientdemographics AS demographics_row
            ON demographics_row.organization_id = enrollment_row.organization_id
           AND demographics_row.patient_id = enrollment_row.patient_id
           AND demographics_row.version = (
               SELECT pg_catalog.max(latest_row.version)
                 FROM clinic_app.intake_patientdemographics AS latest_row
                WHERE latest_row.organization_id = enrollment_row.organization_id
                  AND latest_row.patient_id = enrollment_row.patient_id
           )
         WHERE enrollment_row.clinic_id = requested_clinic
           AND enrollment_row.organization_id = NULLIF(
               pg_catalog.current_setting('app.current_tenant', true), ''
           )::pg_catalog.uuid

      ) AS decrypted
     WHERE\x20
        (pg_catalog.strpos(
            clinic_app.unaccent(
                pg_catalog.lower(decrypted.decrypted_name)
            ),
            clinic_app.unaccent(pg_catalog.lower(normalized_query))
        ) > 0
        OR pg_catalog.strpos(
            clinic_app.unaccent(
                pg_catalog.lower(
                    COALESCE(decrypted.decrypted_social_name, '')
                )
            ),
            clinic_app.unaccent(pg_catalog.lower(normalized_query))
        ) > 0)

       AND (requested_birth IS NULL
            OR decrypted.decrypted_birth = requested_birth)
     ORDER BY pg_catalog.lower(decrypted.decrypted_name),
              decrypted.decrypted_birth,
              decrypted.patient_id
     LIMIT page_limit OFFSET page_offset;
END
$function$;

ALTER FUNCTION clinic_app.patient_registry_count(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date
) OWNER TO clinic_owner;
ALTER FUNCTION clinic_app.patient_registry_page(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date,
    pg_catalog.int4, pg_catalog.int4
) OWNER TO clinic_owner;

REVOKE ALL ON FUNCTION clinic_app.patient_registry_count(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date
) FROM PUBLIC, clinic_resolver;
REVOKE ALL ON FUNCTION clinic_app.patient_registry_page(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date,
    pg_catalog.int4, pg_catalog.int4
) FROM PUBLIC, clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_app.patient_registry_count(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date
) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.patient_registry_page(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date,
    pg_catalog.int4, pg_catalog.int4
) TO clinic_app;
GRANT EXECUTE ON FUNCTION clinic_app.patient_registry_count(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date
) TO clinic_owner;
GRANT EXECUTE ON FUNCTION clinic_app.patient_registry_page(
    pg_catalog.text, pg_catalog.uuid, pg_catalog.text, pg_catalog.date,
    pg_catalog.int4, pg_catalog.int4
) TO clinic_owner;

RESET ROLE;
"""

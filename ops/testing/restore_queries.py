"""Keep the fixed logical-recovery SQL catalog separate from proof logic."""

from typing import Final

EQUALITY_RELATIONS: Final = (
    "audit_event",
    "identity_clinic",
    "identity_organization",
    "identity_user",
    "identity_userclinicrole",
    "intake_patient",
    "intake_patientclinicenrollment",
    "otp_totp_totpdevice",
    "scheduling_appointment",
    "scheduling_availabilityblock",
)
FINGERPRINT_SQL: Final = (
    (
        "audit_event",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) FROM clinic_app.audit_event AS r",
    ),
    (
        "identity_clinic",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) FROM clinic_app.identity_clinic AS r",
    ),
    (
        "identity_organization",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) "
        "FROM clinic_app.identity_organization AS r",
    ),
    (
        "identity_user",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) FROM clinic_app.identity_user AS r",
    ),
    (
        "identity_userclinicrole",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) "
        "FROM clinic_app.identity_userclinicrole AS r",
    ),
    (
        "intake_patient",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) FROM clinic_app.intake_patient AS r",
    ),
    (
        "intake_patientclinicenrollment",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) "
        "FROM clinic_app.intake_patientclinicenrollment AS r",
    ),
    (
        "otp_totp_totpdevice",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) "
        "FROM clinic_app.otp_totp_totpdevice AS r",
    ),
    (
        "scheduling_appointment",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) "
        "FROM clinic_app.scheduling_appointment AS r",
    ),
    (
        "scheduling_availabilityblock",
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        "ORDER BY to_jsonb(r)::text),'')) "
        "FROM clinic_app.scheduling_availabilityblock AS r",
    ),
)
MIGRATION_LEAVES_SQL: Final = """
SELECT app || '|' || name
FROM clinic_app.django_migrations AS candidate
WHERE NOT EXISTS (
    SELECT 1 FROM clinic_app.django_migrations AS later
    WHERE later.app = candidate.app AND later.id > candidate.id
)
ORDER BY app, name
"""
HBA_SQL: Final = """
SELECT type || ' ' || array_to_string(database, ',') || ' ' ||
       array_to_string(user_name, ',') ||
       CASE WHEN type = 'local' THEN ''
            ELSE ' ' || address || '/' ||
              CASE WHEN netmask IN ('0.0.0.0', '::') THEN '0'
                   ELSE masklen(netmask::inet)::text
              END
       END || ' ' || auth_method
FROM pg_hba_file_rules
WHERE error IS NULL
ORDER BY rule_number
"""
POSTURE_SQL: Final = """
WITH violations AS (
    SELECT 'schema-owner' AS kind FROM pg_namespace
    WHERE nspname = 'clinic_app' AND pg_get_userbyid(nspowner) <> 'clinic_owner'
    UNION ALL
    SELECT 'relation-owner' FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='clinic_app' AND c.relkind IN ('r','S')
      AND pg_get_userbyid(c.relowner) <> 'clinic_owner'
    UNION ALL
    SELECT 'function-owner:' || p.proname || ':' || pg_get_userbyid(p.proowner)
    FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
    WHERE n.nspname='clinic_app'
      AND pg_get_userbyid(p.proowner) NOT IN ('clinic_owner','clinic_resolver')
      AND NOT EXISTS (
        SELECT 1 FROM pg_depend d
        WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e'
      )
    UNION ALL
    SELECT 'rls:' || c.relname
    FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace
    WHERE n.nspname='clinic_app' AND c.relkind='r'
      AND c.relname IN (
        'identity_organization','identity_clinic','identity_userclinicrole',
        'otp_totp_totpdevice','intake_patient',
        'intake_patientclinicenrollment','scheduling_availabilityblock',
        'scheduling_appointment','tenancy_tenantprobe'
      ) AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
)
SELECT COALESCE(string_agg(kind, ',' ORDER BY kind), '') FROM violations
"""
SOURCE_SCOPE_SQL: Final = """
SELECT json_build_object(
  'active_writer_count', (
    SELECT count(*) FROM pg_stat_activity
    WHERE datname=current_database() AND pid<>pg_backend_pid() AND state<>'idle'
  ),
  'organization_count', (SELECT count(*) FROM clinic_app.identity_organization),
  'identity_user_count', (SELECT count(*) FROM clinic_app.identity_user),
  'users_without_role_count', (
    SELECT count(*) FROM clinic_app.identity_user AS u
    WHERE NOT EXISTS (
      SELECT 1 FROM clinic_app.identity_userclinicrole AS r WHERE r.user_id=u.id
    )
  ),
  'totp_without_identity_count', (
    SELECT count(*) FROM clinic_app.otp_totp_totpdevice AS d
    WHERE NOT EXISTS (
      SELECT 1 FROM clinic_app.identity_user AS u WHERE u.id=d.user_id
    )
  ),
  'foreign_audit_organization_count', (
    SELECT count(*) FROM clinic_app.audit_event AS e
    WHERE e.organization_id<>'00000000-0000-0000-0000-000000000000'
      AND NOT EXISTS (
        SELECT 1 FROM clinic_app.identity_organization AS o
        WHERE o.id=e.organization_id
      )
  ),
  'identity_user_groups', (SELECT count(*) FROM clinic_app.identity_user_groups),
  'identity_user_user_permissions', (
    SELECT count(*) FROM clinic_app.identity_user_user_permissions
  ),
  'otp_static_staticdevice', (
    SELECT count(*) FROM clinic_app.otp_static_staticdevice
  ),
  'otp_static_statictoken', (
    SELECT count(*) FROM clinic_app.otp_static_statictoken
  ),
  'tenancy_tenantprobe', (SELECT count(*) FROM clinic_app.tenancy_tenantprobe),
  'unexpected_domain_relations', (
    SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]')
    FROM pg_tables
    WHERE schemaname='clinic_app' AND tablename NOT IN (
      'audit_event','identity_clinic','identity_organization','identity_user',
      'identity_userclinicrole','intake_patient','intake_patientclinicenrollment',
      'otp_totp_totpdevice','scheduling_appointment',
      'scheduling_availabilityblock','identity_user_groups',
      'identity_user_user_permissions','otp_static_staticdevice',
      'otp_static_statictoken','tenancy_tenantprobe','auth_group',
      'auth_group_permissions','auth_permission','django_admin_log',
      'django_content_type','django_migrations','django_session'
    )
  )
)
"""

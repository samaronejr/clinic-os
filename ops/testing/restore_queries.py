"""Keep the fixed logical-recovery SQL catalog separate from proof logic.

Every statement is built only from the fixed contract constants; no caller
input ever reaches this catalog, so string composition is closed by
construction.
"""

# ruff: noqa: S608

from typing import Final

from ops.testing.restore_contract import (
    DOMAIN_RELATIONS,
    EXCLUDED_RELATIONS,
    REQUIRED_EMPTY,
)

EQUALITY_RELATIONS: Final = DOMAIN_RELATIONS
RLS_RELATIONS: Final = tuple(
    sorted(
        (
            *(
                relation
                for relation in DOMAIN_RELATIONS
                if relation not in ("audit_event", "identity_user")
            ),
            "tenancy_tenantprobe",
        )
    )
)


def _fingerprint(relation: str) -> str:
    return (
        "SELECT md5(COALESCE(string_agg(to_jsonb(r)::text, E'\\n' "
        f"ORDER BY to_jsonb(r)::text),'')) FROM clinic_app.{relation} AS r"
    )


FINGERPRINT_SQL: Final = tuple(
    (relation, _fingerprint(relation)) for relation in EQUALITY_RELATIONS
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
# Tables whose contents are closed to the runtime role: the append-only
# ledger, the wrapped-DEK store and the discarded-content archive accept
# writes only through owner/resolver functions, so clinic_app must hold no
# table-level privilege on them at all.
CLOSED_TO_APP_RELATIONS: Final = (
    "audit_event",
    "ehr_discarded_content",
    "tenancy_tenantdatakey",
)
POSTURE_SQL: Final = f"""

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
      AND c.relname IN ({",".join(f"'{name}'" for name in RLS_RELATIONS)})
      AND (NOT c.relrowsecurity OR NOT c.relforcerowsecurity)
    UNION ALL
    -- Role-attribute drift: the bootstrap contract pins exactly these flags;
    -- a restored or drifted role with extra privilege fails closed here.
    SELECT 'role:' || rolname || ':superuser' FROM pg_roles
    WHERE rolname IN ('clinic_owner','clinic_app','clinic_resolver')
      AND rolsuper
    UNION ALL
    SELECT 'role:' || rolname || ':bypassrls' FROM pg_roles
    WHERE rolname IN ('clinic_owner','clinic_app') AND rolbypassrls
    UNION ALL
    SELECT 'role:clinic_resolver:not-bypassrls' FROM pg_roles
    WHERE rolname='clinic_resolver' AND NOT rolbypassrls
    UNION ALL
    SELECT 'role:' || rolname || ':login' FROM pg_roles
    WHERE rolname='clinic_resolver' AND rolcanlogin
    UNION ALL
    SELECT 'role:' || rolname || ':missing' FROM (VALUES
      ('clinic_owner'),('clinic_app'),('clinic_resolver'),('clinic_super')
    ) AS expected(rolname)
    WHERE NOT EXISTS (
      SELECT 1 FROM pg_roles r WHERE r.rolname = expected.rolname
    )
    UNION ALL
    SELECT 'role:clinic_super:not-superuser' FROM pg_roles
    WHERE rolname='clinic_super' AND NOT rolsuper
    UNION ALL
    -- Closed-table ACL drift: the runtime role must hold no privilege on
    -- relations written only through owner/resolver functions.
    SELECT 'acl:' || c.relname || ':' || acl.privilege_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(c.relacl) acl
    WHERE n.nspname='clinic_app' AND c.relkind='r'
      AND c.relname IN (
        {",".join(f"'{name}'" for name in CLOSED_TO_APP_RELATIONS)}
      )
      AND acl.grantee = 'clinic_app'::regrole
    UNION ALL
    -- Effective-privilege drift: has_table_privilege includes PUBLIC and
    -- inherited grants, so a PUBLIC grant on a closed table fails here even
    -- though no acl entry names clinic_app.
    SELECT 'acl-effective:' || c.relname || ':' || privilege.privilege_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN (VALUES
      ('SELECT'),('INSERT'),('UPDATE'),('DELETE'),
      ('TRUNCATE'),('REFERENCES'),('TRIGGER')
    ) AS privilege(privilege_type)
    WHERE n.nspname='clinic_app' AND c.relkind='r'
      AND c.relname IN (
        {",".join(f"'{name}'" for name in CLOSED_TO_APP_RELATIONS)}
      )
      AND pg_catalog.has_table_privilege(
        'clinic_app', c.oid, privilege.privilege_type)
    UNION ALL
    -- PUBLIC grants on any domain relation are illegitimate: tenant
    -- isolation must come from RLS policies, not table-level exposure.
    SELECT 'acl-public:' || c.relname || ':' || acl.privilege_type
    FROM pg_class c
    JOIN pg_namespace n ON n.oid=c.relnamespace
    CROSS JOIN LATERAL aclexplode(c.relacl) acl
    WHERE n.nspname='clinic_app' AND c.relkind='r'
      AND c.relname IN (
        {",".join(f"'{name}'" for name in DOMAIN_RELATIONS)}
      )
      AND acl.grantee = 0::pg_catalog.oid
)
SELECT COALESCE(string_agg(kind, ',' ORDER BY kind), '') FROM violations
"""
AUDIT_ROWS_SQL: Final = """
SELECT json_build_object(
  'seq', seq,
  'organization_id', organization_id,
  'actor_user_id', actor_user_id,
  'event_type', event_type,
  'component_id', component_id,
  'component_ip', host(component_ip),
  'affected_record_type', affected_record_type,
  'affected_record_id', affected_record_id,
  'occurred_at_utc', to_char(occurred_at_utc AT TIME ZONE 'UTC',
    'YYYY-MM-DD"T"HH24:MI:SS.US"Z"'),
  'payload', payload,
  'prev_hash', encode(prev_hash, 'hex'),
  'curr_hash', encode(curr_hash, 'hex')
)::text
FROM clinic_app.audit_event
ORDER BY organization_id, seq
"""
EMPTY_TARGET_SQL: Final = f"""
SELECT count(*) FROM (
{" UNION ALL ".join(f"SELECT 1 FROM clinic_app.{name}" for name in DOMAIN_RELATIONS)}
) AS restored_rows
"""
AUDIT_CHAIN_SQL: Final = """
WITH ordered AS (
    SELECT seq, prev_hash, curr_hash,
           lag(curr_hash) OVER (
               PARTITION BY organization_id ORDER BY seq
           ) AS expected_prev
    FROM clinic_app.audit_event
)
SELECT count(*) FROM ordered
WHERE (expected_prev IS NULL
       AND prev_hash <> decode(repeat('00', 32), 'hex'))
   OR (expected_prev IS NOT NULL AND prev_hash <> expected_prev)
"""
# The provider registry is recreated by the target's own migrations under
# fresh surrogate keys, so equality is proven on every column except the
# surrogate ids and timestamps, with foreign keys rewritten to natural keys.
# New columns join the comparison automatically through to_jsonb.
TARGET_SEED_SQL: Final = """
SELECT md5(COALESCE(string_agg(line, E'\\n' ORDER BY line), ''))
FROM (
    SELECT 'capability ' || (
        (to_jsonb(c) - 'id' - 'current_version_id' - 'created_at'
            - 'updated_at')
        || jsonb_build_object(
            'current_version',
            to_jsonb(v) - 'id' - 'capability_id' - 'created_at' - 'updated_at'
        )
    )::text AS line
    FROM clinic_app.providers_providercapability AS c
    LEFT JOIN clinic_app.providers_capabilityversion AS v
      ON v.id = c.current_version_id
    UNION ALL
    SELECT 'version ' || (
        (to_jsonb(v) - 'id' - 'capability_id' - 'created_at' - 'updated_at')
        || jsonb_build_object('capability', jsonb_build_array(c.key, c.clinic_id))
    )::text
    FROM clinic_app.providers_capabilityversion AS v
    JOIN clinic_app.providers_providercapability AS c
      ON c.id = v.capability_id
) AS registry
"""
TENANT_KEY_STATUS_SQL: Final = """
SELECT key_version || '|' || status
FROM clinic_app.tenancy_tenantdatakey
ORDER BY key_version
"""
ATTACHMENT_MANIFEST_SQL: Final = """
SELECT storage_key || '|' || organization_id || '|' || sha256 || '|' ||
  size_bytes
FROM clinic_app.ehr_clinicalattachment
ORDER BY storage_key
"""
SOURCE_SCOPE_SQL: Final = f"""

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
  'tenant_data_key_count', (
    SELECT count(*) FROM clinic_app.tenancy_tenantdatakey
  ),
{
    "".join(
        f"  '{name}', (SELECT count(*) FROM clinic_app.{name}),\n"
        for name in REQUIRED_EMPTY
    )
}  'unexpected_domain_relations', (
    SELECT COALESCE(json_agg(tablename ORDER BY tablename), '[]')
    FROM pg_tables
    WHERE schemaname='clinic_app' AND tablename NOT IN (
      {
    ",".join(
        f"'{name}'"
        for name in sorted(
            (
                *DOMAIN_RELATIONS,
                *EXCLUDED_RELATIONS,
                "auth_group",
                "auth_group_permissions",
                "auth_permission",
            )
        )
    )
}
    )
  )
)
"""

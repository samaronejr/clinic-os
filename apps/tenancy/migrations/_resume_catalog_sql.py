"""Read whole object definitions without physical IDs or runtime statistics."""

CATALOG_SQL = """
WITH rels AS (
    SELECT * FROM pg_catalog.pg_class
    WHERE relnamespace = 'clinic_app'::pg_catalog.regnamespace
      AND relkind NOT IN ('i', 'I')
), cols AS (
    SELECT a.*
    FROM pg_catalog.pg_attribute AS a JOIN rels AS r ON r.oid = attrelid
    WHERE attnum > 0 AND NOT attisdropped AND relkind <> 'S'
), facts AS (
    SELECT 'relation ' || relname AS object_name, ARRAY[]::text[] AS path,
        pg_catalog.jsonb_build_object(
            'kind', relkind, 'persistence', relpersistence,
            'owner', pg_catalog.pg_get_userbyid(relowner),
            'rls', relrowsecurity, 'force_rls', relforcerowsecurity,
            'replica_identity', relreplident, 'options', reloptions,
            'access_method', amname, 'tablespace', spcname,
            'partition_key', pg_catalog.pg_get_partkeydef(r.oid),
            'partition_bound', pg_catalog.pg_get_expr(relpartbound, r.oid),
            'view', CASE WHEN relkind IN ('v', 'm')
                THEN pg_catalog.pg_get_viewdef(r.oid) END
        ) AS definition
    FROM rels AS r
    LEFT JOIN pg_catalog.pg_am ON relam = pg_am.oid
    LEFT JOIN pg_catalog.pg_tablespace ON reltablespace = pg_tablespace.oid
    UNION ALL
    SELECT 'relation ' || relname, ARRAY['columns', attname],
        pg_catalog.jsonb_build_object(
            'type', pg_catalog.format_type(atttypid, NULL),
            'typmod', atttypmod, 'not_null', attnotnull,
            'default', pg_catalog.pg_get_expr(adbin, adrelid),
            'storage', attstorage, 'compression', attcompression,
            'collation', attcollation::pg_catalog.regcollation::text,
            'identity', attidentity, 'generated', attgenerated,
            'options', attoptions, 'foreign_options', attfdwoptions,
            'statistics_target', attstattarget
        )
    FROM cols JOIN rels ON attrelid = rels.oid
    LEFT JOIN pg_catalog.pg_attrdef ON adrelid = attrelid AND adnum = attnum
    UNION ALL
    SELECT 'relation ' || relname, ARRAY['constraints', conname],
        pg_catalog.jsonb_build_object(
            'definition', pg_catalog.pg_get_constraintdef(c.oid),
            'kind', contype, 'validated', convalidated, 'deferrable', condeferrable,
            'deferred', condeferred, 'no_inherit', connoinherit
        )
    FROM pg_catalog.pg_constraint AS c JOIN rels ON conrelid = rels.oid
    UNION ALL
    SELECT 'relation ' || tablename, ARRAY['policies', policyname],
        pg_catalog.jsonb_build_object(
            'permissive', permissive, 'command', cmd, 'using', qual,
            'with_check', with_check,
            'roles', ARRAY(
                SELECT role FROM pg_catalog.unnest(roles) AS role ORDER BY role
            )
        )
    FROM pg_catalog.pg_policies WHERE schemaname = 'clinic_app'
    UNION ALL
    SELECT 'relation ' || relname, ARRAY['triggers', tgname],
        pg_catalog.jsonb_build_object(
            'definition', pg_catalog.pg_get_triggerdef(t.oid), 'enabled', tgenabled
        )
    FROM pg_catalog.pg_trigger AS t JOIN rels ON tgrelid = rels.oid
    WHERE NOT tgisinternal
    UNION ALL
    SELECT 'relation ' || r.relname, ARRAY['indexes', i.relname],
        pg_catalog.jsonb_build_object(
            'definition', pg_catalog.pg_get_indexdef(indexrelid), 'valid', indisvalid,
            'ready', indisready, 'live', indislive, 'clustered', indisclustered,
            'replica_identity', indisreplident, 'options', i.reloptions,
            'tablespace', spcname
        )
    FROM pg_catalog.pg_index JOIN rels AS r ON indrelid = r.oid
    JOIN pg_catalog.pg_class AS i ON indexrelid = i.oid
    LEFT JOIN pg_catalog.pg_tablespace ON i.reltablespace = pg_tablespace.oid
    UNION ALL
    SELECT 'relation ' || relname, ARRAY['sequence'],
        pg_catalog.jsonb_build_object(
            'type', pg_catalog.format_type(seqtypid, NULL), 'start', seqstart,
            'increment', seqincrement, 'min', seqmin, 'max', seqmax,
            'cache', seqcache, 'cycle', seqcycle
        )
    FROM pg_catalog.pg_sequence JOIN rels ON seqrelid = rels.oid
    UNION ALL
    SELECT 'function ' || proname || '(' ||
        pg_catalog.oidvectortypes(proargtypes) || ')', ARRAY[]::text[],
        pg_catalog.jsonb_build_object(
            'definition', pg_catalog.pg_get_functiondef(p.oid),
            'owner', pg_catalog.pg_get_userbyid(proowner), 'leakproof', proleakproof
        )
    FROM pg_catalog.pg_proc AS p
    WHERE pronamespace = 'clinic_app'::pg_catalog.regnamespace AND prokind IN ('f', 'p')
    UNION ALL
    SELECT 'extension ' || extname, ARRAY[]::text[],
        pg_catalog.jsonb_build_object(
            'schema', nspname, 'version', extversion,
            'owner', pg_catalog.pg_get_userbyid(extowner)
        )
    FROM pg_catalog.pg_extension
    JOIN pg_catalog.pg_namespace ON extnamespace = pg_namespace.oid
    WHERE extname = 'unaccent'
), privileges AS (
    SELECT 'relation ' || relname AS object_name, ARRAY['grants'] AS path,
        g.*
    FROM rels CROSS JOIN LATERAL pg_catalog.aclexplode(
        COALESCE(relacl, pg_catalog.acldefault(
            CASE WHEN relkind = 'S' THEN 's'::"char" ELSE 'r'::"char" END, relowner
        ))
    ) AS g
    UNION ALL
    SELECT 'relation ' || relname, ARRAY['columns', attname, 'grants'], g.*
    FROM cols JOIN rels ON rels.oid = attrelid
    CROSS JOIN LATERAL pg_catalog.aclexplode(attacl) AS g
    UNION ALL
    SELECT 'function ' || proname || '(' ||
        pg_catalog.oidvectortypes(proargtypes) || ')', ARRAY['grants'], g.*
    FROM pg_catalog.pg_proc AS p
    CROSS JOIN LATERAL pg_catalog.aclexplode(
        COALESCE(proacl, pg_catalog.acldefault('f', proowner))
    ) AS g
    WHERE pronamespace = 'clinic_app'::pg_catalog.regnamespace AND prokind IN ('f', 'p')
), grants AS (
    SELECT object_name, path, pg_catalog.jsonb_object_agg(
        CASE WHEN grantee = 0 THEN 'PUBLIC' ELSE pg_catalog.pg_get_userbyid(grantee) END
        || ':' || privilege_type || ':' || pg_catalog.pg_get_userbyid(grantor),
        is_grantable
    ) AS definition
    FROM privileges GROUP BY object_name, path
)
SELECT object_name, path, definition::text FROM facts
UNION ALL
SELECT object_name, path, definition::text FROM grants
"""

# PostgreSQL-generated FK trigger names contain local OIDs. The logical key
# instead identifies the owning constraint, the target and the RI function.
FK_TRIGGERS_SQL = """
SELECT 'relation ' || target.relname,
       ARRAY['constraint_triggers', source.relname || '.' || conname || '.' || proname],
       pg_catalog.jsonb_build_object(
           'enabled', tgenabled, 'event', tgtype,
           'deferrable', tgdeferrable, 'deferred', tginitdeferred
       )::text
FROM pg_catalog.pg_trigger AS t
JOIN pg_catalog.pg_constraint AS c ON c.oid = tgconstraint
JOIN pg_catalog.pg_class AS source ON source.oid = conrelid
JOIN pg_catalog.pg_class AS target ON target.oid = tgrelid
JOIN pg_catalog.pg_proc ON pg_proc.oid = tgfoid
WHERE tgisinternal AND target.relnamespace = 'clinic_app'::pg_catalog.regnamespace
"""

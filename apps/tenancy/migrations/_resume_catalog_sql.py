"""OID-independent definitions for the PostgreSQL 16 resume contracts."""

CATALOG_SQL = """
WITH rels AS (
    SELECT * FROM pg_catalog.pg_class
    WHERE relnamespace = 'clinic_app'::pg_catalog.regnamespace
), cols AS (
    SELECT r.oid, r.relname, a.*
    FROM rels AS r JOIN pg_catalog.pg_attribute AS a ON a.attrelid = r.oid
    WHERE a.attnum > 0 AND NOT a.attisdropped AND r.relkind IN ('r', 'p', 'v', 'm')
), funcs AS (
    SELECT p.*, p.proname || '(' ||
        pg_catalog.oidvectortypes(p.proargtypes) || ')' AS signature
    FROM pg_catalog.pg_proc AS p
    WHERE pronamespace = 'clinic_app'::pg_catalog.regnamespace AND prokind IN ('f', 'p')
), facts AS (
    SELECT 'relation ' || relname AS object_name,
           pg_catalog.jsonb_build_array(relkind, relpersistence, relispartition,
               pg_catalog.pg_get_userbyid(relowner), reloptions,
               CASE WHEN relkind IN ('v', 'm') THEN pg_catalog.pg_get_viewdef(oid) END
           ) AS definition
    FROM rels
    UNION ALL
    SELECT 'RLS flags ' || relname,
           pg_catalog.jsonb_build_array(relrowsecurity, relforcerowsecurity)
    FROM rels WHERE relkind IN ('r', 'p')
    UNION ALL
    SELECT 'column ' || relname || '.' || attname,
           pg_catalog.jsonb_build_array(pg_catalog.format_type(atttypid, atttypmod),
               attnotnull, attidentity, attgenerated,
               attcollation::pg_catalog.regcollation::text,
               pg_catalog.pg_get_expr(adbin, adrelid))
    FROM cols
    LEFT JOIN pg_catalog.pg_attrdef ON adrelid = attrelid AND adnum = attnum
    UNION ALL
    SELECT 'policy ' || tablename || '.' || policyname,
           pg_catalog.jsonb_build_array(permissive, cmd, qual, with_check,
               ARRAY(SELECT role FROM pg_catalog.unnest(roles) AS role ORDER BY role))
    FROM pg_catalog.pg_policies WHERE schemaname = 'clinic_app'
    UNION ALL
    SELECT 'constraint ' || relname || '.' || conname,
           pg_catalog.jsonb_build_array(contype, convalidated, condeferrable,
               condeferred, connoinherit, pg_catalog.pg_get_constraintdef(c.oid))
    FROM pg_catalog.pg_constraint AS c JOIN rels ON conrelid = rels.oid
    UNION ALL
    SELECT 'trigger ' || relname || '.' || tgname,
           pg_catalog.jsonb_build_array(tgenabled, pg_catalog.pg_get_triggerdef(t.oid))
    FROM pg_catalog.pg_trigger AS t JOIN rels ON tgrelid = rels.oid
    WHERE NOT tgisinternal
    UNION ALL
    SELECT 'index ' || i.relname,
           pg_catalog.jsonb_build_array(indisvalid, indisready, indislive,
               pg_catalog.pg_get_indexdef(indexrelid))
    FROM pg_catalog.pg_index JOIN rels AS i ON i.oid = indexrelid
    UNION ALL
    SELECT 'function ' || signature,
           pg_catalog.jsonb_build_array(pg_catalog.pg_get_functiondef(p.oid),
               pg_catalog.pg_get_userbyid(proowner), proleakproof)
    FROM funcs AS p
    UNION ALL
    SELECT 'extension ' || extname,
           pg_catalog.jsonb_build_array(nspname, extversion,
               pg_catalog.pg_get_userbyid(extowner))
    FROM pg_catalog.pg_extension
    JOIN pg_catalog.pg_namespace ON extnamespace = pg_namespace.oid
    WHERE extname = 'unaccent'
    UNION ALL
    SELECT 'grants ' || relname,
           pg_catalog.to_jsonb(ARRAY(
               SELECT acl::text FROM pg_catalog.unnest(
                   COALESCE(relacl, pg_catalog.acldefault(
                       CASE WHEN relkind = 'S' THEN 's'::"char" ELSE 'r'::"char" END,
                       relowner
                   ))
               ) AS acl ORDER BY acl::text COLLATE "C"
           ))
    FROM rels WHERE relkind IN ('r', 'p', 'v', 'm', 'S')
    UNION ALL
    SELECT 'column grants ' || r.relname,
           pg_catalog.to_jsonb(ARRAY(
               SELECT a.attname || ':' || acl::text
               FROM cols AS a CROSS JOIN LATERAL pg_catalog.unnest(a.attacl) AS acl
               WHERE a.attrelid = r.oid
               ORDER BY (a.attname || ':' || acl::text) COLLATE "C"
           ))
    FROM rels AS r WHERE relkind IN ('r', 'p', 'v', 'm')
    UNION ALL
    SELECT 'function grants ' || signature,
           pg_catalog.to_jsonb(ARRAY(
               SELECT acl::text FROM pg_catalog.unnest(
                   COALESCE(proacl, pg_catalog.acldefault('f', proowner))
               ) AS acl ORDER BY acl::text COLLATE "C"
           ))
    FROM funcs
)
SELECT object_name, definition::text FROM facts ORDER BY object_name COLLATE "C"
"""

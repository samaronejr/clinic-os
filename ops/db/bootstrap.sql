\set ON_ERROR_STOP on
\getenv clinic_owner_password CLINIC_OWNER_PASSWORD
\getenv clinic_app_password CLINIC_APP_PASSWORD
\getenv clinic_super_password CLINIC_SUPER_PASSWORD
-- Optional machine credential: absent means password authentication is disabled.
\set clinic_agent_password ''
\getenv clinic_agent_password CLINIC_AGENT_PASSWORD

SELECT format(
    'CREATE ROLE clinic_owner LOGIN PASSWORD %L',
    :'clinic_owner_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clinic_owner')
\gexec

SELECT format(
    'CREATE ROLE clinic_app LOGIN PASSWORD %L',
    :'clinic_app_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clinic_app')
\gexec

SELECT 'CREATE ROLE clinic_agent LOGIN NOSUPERUSER NOBYPASSRLS NOINHERIT'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clinic_agent')
\gexec

SELECT format(
    'ALTER ROLE clinic_agent WITH LOGIN PASSWORD %L NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION NOINHERIT',
    NULLIF(:'clinic_agent_password', '')
)
\gexec

SELECT 'CREATE ROLE clinic_resolver'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clinic_resolver')
\gexec

SELECT format(
    'CREATE ROLE clinic_super LOGIN PASSWORD %L',
    :'clinic_super_password'
)
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clinic_super')
\gexec

ALTER ROLE clinic_owner WITH LOGIN PASSWORD :'clinic_owner_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;
ALTER ROLE clinic_app WITH LOGIN PASSWORD :'clinic_app_password' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;
ALTER ROLE clinic_resolver WITH NOLOGIN NOSUPERUSER BYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION INHERIT;
ALTER ROLE clinic_super WITH LOGIN PASSWORD :'clinic_super_password' SUPERUSER;

ALTER DATABASE :"database_name" OWNER TO clinic_owner;
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO clinic_owner, clinic_app, clinic_agent, clinic_super;

CREATE SCHEMA IF NOT EXISTS :"app_schema" AUTHORIZATION clinic_owner;
ALTER SCHEMA :"app_schema" OWNER TO clinic_owner;
REVOKE ALL ON SCHEMA :"app_schema" FROM PUBLIC, clinic_app, clinic_agent, clinic_resolver;
GRANT USAGE ON SCHEMA :"app_schema" TO clinic_owner, clinic_app, clinic_agent;
GRANT USAGE, CREATE ON SCHEMA :"app_schema" TO clinic_resolver;

GRANT clinic_app TO clinic_owner WITH INHERIT FALSE, SET TRUE;
GRANT clinic_resolver TO clinic_owner WITH INHERIT FALSE, SET TRUE;

ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA :"app_schema"
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO clinic_app;
ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA :"app_schema"
    GRANT USAGE ON SEQUENCES TO clinic_app;

-- BEGIN migration witness bootstrap (also installed by the scratch DB factory)
BEGIN;
CREATE SCHEMA IF NOT EXISTS clinic_heap_inspect;
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT FROM pg_catalog.pg_namespace n JOIN pg_catalog.pg_roles r
          ON r.oid = n.nspowner
        WHERE n.nspname = 'clinic_heap_inspect' AND r.rolsuper
    ) THEN
        RAISE EXCEPTION 'migration heap inspection requires an administrator-owned schema';
    END IF;
END $$;
REVOKE ALL ON SCHEMA clinic_heap_inspect FROM PUBLIC, clinic_app, clinic_agent,
    clinic_resolver, clinic_owner;
CREATE EXTENSION IF NOT EXISTS pageinspect WITH SCHEMA clinic_heap_inspect;
REVOKE ALL ON ALL FUNCTIONS IN SCHEMA clinic_heap_inspect FROM PUBLIC, clinic_app,
    clinic_agent, clinic_owner, clinic_resolver;

-- pageinspect itself is superuser-only. Export *only* line pointers and
-- physical attribute counts, never tuple data. No caller-provided SQL, table
-- callbacks or user-defined types execute in the privileged function.
CREATE OR REPLACE FUNCTION clinic_heap_inspect.short_tuples(target oid, first_block bigint, after_block bigint)
RETURNS TABLE (location tid, natts integer)
LANGUAGE sql STRICT VOLATILE SECURITY DEFINER SET search_path = pg_catalog, pg_temp
AS $$
    SELECT format('(%s,%s)', b.nr, h.lp)::tid, h.t_infomask2 & 2047
    FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace
    JOIN pg_am am ON am.oid = c.relam
    CROSS JOIN LATERAL (
        SELECT max(attnum) AS last_missing FROM pg_attribute
        WHERE attrelid = c.oid AND atthasmissing AND NOT attisdropped
    ) a
    CROSS JOIN LATERAL generate_series(greatest(first_block, 0::bigint),
        least(after_block, pg_relation_size(c.oid::regclass) / current_setting('block_size')::bigint) - 1
    ) b(nr)
    CROSS JOIN LATERAL clinic_heap_inspect.heap_page_items(
        clinic_heap_inspect.get_raw_page(format('%I.%I', n.nspname, c.relname), b.nr)
    ) h
    WHERE c.oid = target AND n.nspname = 'clinic_app' AND c.relkind = 'r'
      AND c.relowner = 'clinic_owner'::regrole AND am.amname = 'heap'
      AND h.lp_flags = 1 AND (h.t_infomask2 & 2047) < a.last_missing
$$;
REVOKE ALL ON FUNCTION clinic_heap_inspect.short_tuples(oid,bigint,bigint) FROM PUBLIC, clinic_app,
    clinic_agent;
GRANT USAGE ON SCHEMA clinic_heap_inspect TO clinic_owner, clinic_resolver;
GRANT EXECUTE ON FUNCTION clinic_heap_inspect.short_tuples(oid,bigint,bigint) TO clinic_owner, clinic_resolver;

SET LOCAL ROLE clinic_owner;
CREATE TABLE IF NOT EXISTS clinic_app.migration_default_witness (
    migration text NOT NULL, relation_name name NOT NULL, column_name name NOT NULL,
    column_type text NOT NULL, type_modifier integer NOT NULL, side_value bytea NOT NULL,
    key_count bigint NOT NULL CHECK (key_count >= 0),
    PRIMARY KEY (migration, relation_name, column_name)
);
CREATE TABLE IF NOT EXISTS clinic_app.migration_default_witness_key (
    migration text NOT NULL, relation_name name NOT NULL, column_name name NOT NULL,
    row_key bytea NOT NULL,
    PRIMARY KEY (migration, relation_name, column_name, row_key),
    FOREIGN KEY (migration, relation_name, column_name)
        REFERENCES clinic_app.migration_default_witness
);
-- Bootstrap is reapplied to existing installations. Old evidence remains
-- local to its migration until a protected transaction actually verifies it.
ALTER TABLE clinic_app.migration_default_witness
    ADD COLUMN IF NOT EXISTS verified_relid oid,
    ADD COLUMN IF NOT EXISTS verified_attnum smallint;
REVOKE ALL ON clinic_app.migration_default_witness, clinic_app.migration_default_witness_key
    FROM PUBLIC, clinic_app, clinic_agent, clinic_resolver;
GRANT SELECT ON clinic_app.migration_default_witness, clinic_app.migration_default_witness_key
    TO clinic_resolver;
COMMIT;
-- END migration witness bootstrap

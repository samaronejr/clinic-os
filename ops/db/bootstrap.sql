\set ON_ERROR_STOP on
\getenv clinic_owner_password CLINIC_OWNER_PASSWORD
\getenv clinic_app_password CLINIC_APP_PASSWORD
\getenv clinic_super_password CLINIC_SUPER_PASSWORD
-- Optional machine credential: absent means password authentication is disabled.
\set clinic_agent_password ''
\getenv clinic_agent_password CLINIC_AGENT_PASSWORD
\set clinic_posture_password ''
\getenv clinic_posture_password CLINIC_POSTURE_PASSWORD

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

-- Only an explicitly provisioned inspection credential enables the audit
-- window's prerequisites. Never open a closed non-template database here.
SELECT 'CREATE ROLE clinic_posture NOINHERIT'
WHERE NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'clinic_posture')
\gexec
SELECT format(
    'ALTER ROLE clinic_posture LOGIN NOINHERIT NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD %L',
    NULLIF(:'clinic_posture_password', '')
)
\gexec

-- Server-side binding keeps values out of statement text. Keep them out of
-- statement/duration/sample detail too, even if server sampling is enabled.
REVOKE SET ON PARAMETER log_parameter_max_length, log_error_verbosity
    FROM PUBLIC, clinic_agent, clinic_app, clinic_owner;
SELECT format('ALTER ROLE %I SET log_parameter_max_length = 0', role_name),
       format('ALTER ROLE %I SET log_parameter_max_length_on_error = 0', role_name),
       -- PostgreSQL 16 lets the login set the on-error GUC in its own
       -- session. TERSE suppresses bind error context in stderr/syslog;
       -- db-posture rejects CSV/JSON destinations, which ignore TERSE here.
       format('ALTER ROLE %I SET log_error_verbosity = terse', role_name)
FROM (
    SELECT rolname AS role_name FROM pg_catalog.pg_roles
    WHERE rolname IN ('clinic_agent', 'clinic_app', 'clinic_owner')
       OR (rolcanlogin AND rolname ~ '^clinic_agent_[a-z0-9_]+$')
) AS roles
\gexec

ALTER DATABASE :"database_name" OWNER TO clinic_owner;
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO clinic_owner, clinic_app, clinic_agent, clinic_super;
GRANT CONNECT ON DATABASE :"database_name" TO clinic_posture;

-- A stock template0 is not inspectable even by a superuser. Do not change
-- it in the read-only gate. In explicitly provisioned clusters, remove
-- PUBLIC access *before* opening it; refuse unexpected pre-existing grants.
SELECT (:'clinic_posture_password' <> '') AS provision_inspection
\gset
\if :provision_inspection
BEGIN;
REVOKE ALL ON DATABASE template0 FROM PUBLIC;
GRANT CONNECT ON DATABASE template0 TO clinic_posture;
DO $$
BEGIN
    IF EXISTS (
        SELECT FROM pg_database d, LATERAL aclexplode(d.datacl) a
        WHERE d.datname = 'template0' AND a.privilege_type = 'CONNECT'
          AND a.grantee NOT IN (d.datdba, 'clinic_posture'::regrole)
    ) OR EXISTS (
        SELECT FROM pg_database d JOIN pg_roles r ON r.oid = d.datdba
        WHERE d.datname = 'template0' AND NOT r.rolsuper
    ) THEN
        RAISE EXCEPTION 'unexpected template0 CONNECT authority';
    END IF;
END $$;
ALTER DATABASE template0 ALLOW_CONNECTIONS true;
COMMIT;
\endif

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

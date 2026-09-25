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

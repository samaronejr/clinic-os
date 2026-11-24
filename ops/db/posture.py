"""Authenticate and verify the environment-supplied runtime database URL."""

from __future__ import annotations

import os
import sys
from typing import Final

import psycopg
from psycopg.conninfo import conninfo_to_dict

POSTURE_QUERY: Final = """
SELECT
    current_user = 'clinic_app'
    AND current_database() = %s
    AND current_database() <> %s
    AND EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = current_user
          AND NOT rolsuper
          AND NOT rolbypassrls
    )
    AND EXISTS (
        SELECT 1
        FROM pg_roles
        WHERE rolname = 'clinic_owner'
          AND NOT rolsuper
          AND NOT rolbypassrls
          AND NOT rolcreatedb
    )
    AND EXISTS (
        SELECT 1 FROM pg_roles
        WHERE rolname = 'clinic_agent' AND rolcanlogin
          AND NOT rolsuper AND NOT rolbypassrls AND NOT rolinherit
          AND NOT rolcreatedb AND NOT rolcreaterole AND NOT rolreplication
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_auth_members WHERE member = 'clinic_agent'::regrole
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_class WHERE relowner = 'clinic_agent'::regrole
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_namespace WHERE nspowner = 'clinic_agent'::regrole
    )
    AND NOT EXISTS (
        SELECT 1 FROM pg_database WHERE datdba = 'clinic_agent'::regrole
    )
    AND EXISTS (
        SELECT 1
        FROM pg_database
        WHERE datname = current_database()
          AND pg_get_userbyid(datdba) = 'clinic_owner'
    )
    AND EXISTS (
        SELECT 1
        FROM pg_namespace
        WHERE nspname = 'clinic_app'
          AND pg_get_userbyid(nspowner) = 'clinic_owner'
    )
    AND (SELECT count(*)=2 AND bool_and(
        c.relkind='r' AND c.relowner='clinic_owner'::regrole AND
        NOT has_any_column_privilege(current_user,c.oid,'SELECT,INSERT,UPDATE') AND
        NOT has_table_privilege(current_user,c.oid,'DELETE,TRUNCATE,TRIGGER') AND
        has_table_privilege('clinic_resolver',c.oid,'SELECT') AND
        NOT has_any_column_privilege('clinic_resolver',c.oid,'INSERT,UPDATE') AND
        NOT has_table_privilege('clinic_resolver',c.oid,'DELETE,TRUNCATE,TRIGGER'))
        FROM pg_class c WHERE c.relnamespace='clinic_app'::regnamespace
        AND c.relname IN ('migration_default_witness','migration_default_witness_key'))
    AND EXISTS (
        SELECT FROM pg_namespace n JOIN pg_extension e ON e.extnamespace=n.oid
        JOIN pg_roles owner ON owner.oid=n.nspowner
        WHERE n.nspname='clinic_heap_inspect' AND e.extname='pageinspect'
        AND owner.rolsuper
        AND NOT has_schema_privilege(current_user,n.oid,'USAGE,CREATE')
        AND NOT has_schema_privilege('clinic_resolver',n.oid,'CREATE')
        AND NOT has_schema_privilege('clinic_owner',n.oid,'CREATE')
    )
    AND (SELECT count(*)=1 AND bool_and(
        p.proname='short_tuples' AND p.proargtypes='26 20 20'::oidvector
        AND p.prosecdef AND p.proisstrict AND owner.rolsuper
        AND p.proconfig=ARRAY['search_path=pg_catalog, pg_temp']
        AND has_function_privilege('clinic_owner',p.oid,'EXECUTE')
        AND has_function_privilege('clinic_resolver',p.oid,'EXECUTE'))
        FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        JOIN pg_roles owner ON owner.oid=p.proowner
        WHERE n.nspname='clinic_heap_inspect' AND NOT EXISTS (
            SELECT FROM pg_depend d WHERE d.classid='pg_proc'::regclass
              AND d.objid=p.oid AND d.deptype='e'))
    AND NOT EXISTS (
        SELECT FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
        WHERE n.nspname='clinic_heap_inspect' AND (
            has_function_privilege(current_user,p.oid,'EXECUTE')
            OR has_function_privilege('clinic_agent',p.oid,'EXECUTE')
            OR (p.proname<>'short_tuples' AND
                (has_function_privilege('clinic_owner',p.oid,'EXECUTE')
                 OR has_function_privilege('clinic_resolver',p.oid,'EXECUTE')))))
    AND EXISTS (
        SELECT 1
        FROM pg_database
        WHERE datname = %s
          AND pg_get_userbyid(datdba) = 'clinic_owner'
    )
"""
# Roles POSTURE_QUERY pins by name. The settings pin covers them and every role
# they can assume (pg_auth_members, followed transitively in the live catalog).
PINNED_ROLES: Final = ("clinic_app", "clinic_owner")
RUNTIME_ROLE_SETTINGS: Final = (
    "idle_in_transaction_session_timeout=15s",
    "search_path=clinic_app, public",
)
# Every pg_db_role_setting row that can apply to a pinned role in this database:
# role-level (setdatabase = 0), role-in-database, database-wide (setrole = 0)
# and ALTER ROLE ALL. Rows are (role or '', database-scoped, sorted settings).
ROLE_SETTINGS_QUERY: Final = """
WITH RECURSIVE family(oid) AS (
    SELECT oid FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
    UNION
    SELECT membership.roleid
    FROM pg_catalog.pg_auth_members AS membership
    JOIN family ON membership.member = family.oid
)
SELECT
    COALESCE(role.rolname, ''),
    setting.setdatabase <> 0,
    ARRAY(
        SELECT item FROM unnest(setting.setconfig) AS item ORDER BY item COLLATE "C"
    )
FROM pg_catalog.pg_db_role_setting AS setting
LEFT JOIN pg_catalog.pg_roles AS role ON role.oid = setting.setrole
WHERE setting.setdatabase IN (
        0,
        (SELECT oid FROM pg_catalog.pg_database WHERE datname = current_database())
    )
  AND (setting.setrole = 0 OR setting.setrole IN (SELECT oid FROM family))
ORDER BY 1, 2
"""
EXPECTED_ROLE_SETTINGS: Final = [("clinic_app", False, list(RUNTIME_ROLE_SETTINGS))]
# The session this check opens is itself a fresh runtime session; its effective
# values catch any precedence path the catalog comparison does not model.
EFFECTIVE_SETTINGS_QUERY: Final = (
    "SELECT current_setting('search_path'), "
    "current_setting('idle_in_transaction_session_timeout')"
)
EXPECTED_EFFECTIVE_SETTINGS: Final = ("clinic_app, public", "15s")
FAILURE_MESSAGE: Final = "database posture check failed"


def _verify_database_posture(database_url: str, test_database_name: str) -> bool:
    try:
        connection_settings = conninfo_to_dict(database_url)
        required_settings = (
            connection_settings.get("user") == "clinic_app"
            and bool(connection_settings.get("password"))
            and bool(
                connection_settings.get("host") or connection_settings.get("hostaddr")
            )
            and bool(connection_settings.get("dbname"))
        )
        if not required_settings:
            return False
        database_name = connection_settings["dbname"]
        with psycopg.connect(database_url, connect_timeout=5) as connection:
            effective = connection.execute(EFFECTIVE_SETTINGS_QUERY).fetchone()
            result = connection.execute(
                POSTURE_QUERY,
                (database_name, test_database_name, test_database_name),
            ).fetchone()
            role_settings = [
                (role, scoped, list(settings))
                for role, scoped, settings in connection.execute(
                    ROLE_SETTINGS_QUERY, (list(PINNED_ROLES),)
                ).fetchall()
            ]
    except (psycopg.Error, ValueError):
        return False
    return (
        result == (True,)
        and effective == EXPECTED_EFFECTIVE_SETTINGS
        and role_settings == EXPECTED_ROLE_SETTINGS
    )


def _main() -> int:
    database_url = os.environ.get("APP_DATABASE_URL", "")
    test_database_name = os.environ.get("TEST_DATABASE_NAME", "")
    if not _verify_database_posture(database_url, test_database_name):
        sys.stderr.write(f"{FAILURE_MESSAGE}\n")
        return 1
    sys.stdout.write(
        "database posture: clinic_app runtime; clinic_agent NOINHERIT NOBYPASSRLS; "
        f"clinic_owner NOCREATEDB; {test_database_name} precreated\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

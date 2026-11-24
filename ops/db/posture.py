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
    AND NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_roles AS r
        WHERE (r.rolname IN ('clinic_agent', 'clinic_app', 'clinic_owner')
               OR (r.rolcanlogin AND r.rolname ~ '^clinic_agent_[a-z0-9_]+$'))
          AND (has_parameter_privilege(r.oid, 'log_parameter_max_length', 'SET')
               OR has_parameter_privilege(r.oid, 'log_error_verbosity', 'SET'))
    )
    AND regexp_split_to_array(
        current_setting('log_destination'), '[[:space:]]*,[[:space:]]*'
    ) <@ ARRAY['stderr', 'syslog']
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
    AND EXISTS (
        SELECT 1
        FROM pg_database
        WHERE datname = %s
          AND pg_get_userbyid(datdba) = 'clinic_owner'
    )
"""
# Roles POSTURE_QUERY pins by name. The settings pin covers them and every role
# they can assume (pg_auth_members, followed transitively in the live catalog).
PINNED_ROLES: Final = ("clinic_agent", "clinic_app", "clinic_owner")
MACHINE_LOGINS_QUERY: Final = """
SELECT rolname FROM pg_catalog.pg_roles
WHERE rolcanlogin AND rolname ~ '^clinic_agent_[a-z0-9_]+$'
"""
PARAMETER_LOG_SETTINGS: Final = (
    "log_error_verbosity=terse",
    "log_parameter_max_length=0",
    "log_parameter_max_length_on_error=0",
)
RUNTIME_ROLE_SETTINGS: Final = (
    "idle_in_transaction_session_timeout=15s",
    *PARAMETER_LOG_SETTINGS,
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
EXPECTED_ROLE_SETTINGS: Final = [
    ("clinic_agent", False, list(PARAMETER_LOG_SETTINGS)),
    ("clinic_app", False, list(RUNTIME_ROLE_SETTINGS)),
    ("clinic_owner", False, list(PARAMETER_LOG_SETTINGS)),
]
# The session this check opens is itself a fresh runtime session; its effective
# values catch any precedence path the catalog comparison does not model.
EFFECTIVE_SETTINGS_QUERY: Final = (
    "SELECT current_setting('search_path'), "
    "current_setting('idle_in_transaction_session_timeout'), "
    "current_setting('log_error_verbosity'), "
    "current_setting('log_parameter_max_length'), "
    "current_setting('log_parameter_max_length_on_error')"
)
EXPECTED_EFFECTIVE_SETTINGS: Final = ("clinic_app, public", "15s", "terse", "0", "0")
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
            machines = [
                name for (name,) in connection.execute(MACHINE_LOGINS_QUERY).fetchall()
            ]
            role_settings = [
                (role, scoped, list(settings))
                for role, scoped, settings in connection.execute(
                    ROLE_SETTINGS_QUERY, ([*PINNED_ROLES, *machines],)
                ).fetchall()
            ]
    except (psycopg.Error, ValueError):
        return False
    return (
        result == (True,)
        and effective == EXPECTED_EFFECTIVE_SETTINGS
        and sorted(role_settings)
        == sorted(
            [
                *EXPECTED_ROLE_SETTINGS,
                *((name, False, list(PARAMETER_LOG_SETTINGS)) for name in machines),
            ]
        )
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

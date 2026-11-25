"""Authenticate and verify the environment-supplied runtime database URL."""

from __future__ import annotations

import os
import sys
from functools import cache
from pathlib import Path
from typing import Final

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict

POSTURE_QUERY: Final = """
SELECT
    current_user = 'clinic_app'
    AND current_database() = %s
    AND current_database() <> %s
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
# The same complete membership graph is checked for role settings and authority.
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
# Deliberately follow *all* membership edges. A non-inherited role can still
# be assumed, and its own grants/memberships then matter. UNION deduplicates
# diamonds. An inert edge to a privileged role is not a supported exception.
ROLE_FAMILY: Final = sql.SQL("""
WITH RECURSIVE membership(member, roleid) AS (
    SELECT member, roleid FROM pg_catalog.pg_auth_members
    UNION ALL
    SELECT datdba, 'pg_database_owner'::regrole::oid
    FROM pg_catalog.pg_database WHERE datname = pg_catalog.current_database()
), family(login, oid) AS (
    SELECT rolname, oid FROM pg_catalog.pg_roles WHERE rolname = ANY(%s)
    UNION
    SELECT family.login, membership.roleid
    FROM membership
    JOIN family ON membership.member = family.oid
)
""")
# These logins need no delegated parameter ACL at all. Check actual ACL
# entries, including PUBLIC, without relying on a list of installed GUCs.
# Ordinary USERSET operations do not need (or create) a parameter ACL.
SAFE_ROLE_FAMILY_QUERY: Final = ROLE_FAMILY + sql.SQL("""
SELECT NOT EXISTS (
    SELECT FROM family JOIN pg_catalog.pg_roles AS r USING (oid)
    WHERE r.rolsuper
       OR r.rolcreaterole
       OR r.rolreplication
       OR (r.rolbypassrls AND NOT (
           family.login = 'clinic_owner' AND r.rolname = 'clinic_resolver'
           AND NOT r.rolcanlogin
       ))
       OR r.rolname IN ('pg_read_server_files', 'pg_write_server_files',
                        'pg_execute_server_program', 'pg_signal_backend')
       OR pg_catalog.pg_has_role('pg_monitor', r.oid, 'MEMBER')
       OR EXISTS (
           SELECT FROM pg_catalog.pg_parameter_acl AS p
           WHERE pg_catalog.has_parameter_privilege(
               r.oid, p.parname, 'SET, ALTER SYSTEM'
           )
       )
)
""")
# Match EXECUTE ACL grantees against that database's complete family. PUBLIC
# (OID zero) and superuser are the two cases that do not require an edge.
RESTRICTED_ROUTINES: Final = ROLE_FAMILY + sql.SQL("""
, extension_members AS MATERIALIZED (
    SELECT p.oid, p.pronamespace FROM pg_depend d JOIN pg_proc p ON p.oid = d.objid
    WHERE d.classid = 'pg_proc'::regclass AND d.refclassid = 'pg_extension'::regclass
      AND d.deptype = 'e' AND d.objsubid = 0
), restricted AS MATERIALIZED (
    SELECT p.*, n.nspname, n.nspowner, p.oid::regprocedure::text AS signature,
           ARRAY(SELECT a.grantee
                 FROM aclexplode(COALESCE(p.proacl, acldefault('f', p.proowner))) a
                 WHERE a.privilege_type = 'EXECUTE') AS executors
    FROM pg_proc p JOIN pg_namespace n ON n.oid = p.pronamespace
    LEFT JOIN pg_init_privs i ON i.classoid = 'pg_proc'::regclass
                             AND i.objoid = p.oid AND i.objsubid = 0
    WHERE (n.nspname = 'pg_catalog'
           OR n.oid IN (SELECT extnamespace FROM pg_extension)
           OR n.oid IN (SELECT pronamespace FROM extension_members))
      AND (
          (p.proacl IS NOT NULL AND NOT EXISTS (
              SELECT FROM aclexplode(p.proacl) a
              WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
          ))
          OR (i.initprivs IS NOT NULL AND NOT EXISTS (
              SELECT FROM aclexplode(i.initprivs) a
              WHERE a.grantee = 0 AND a.privilege_type = 'EXECUTE'
          ))
          OR (n.nspname = 'clinic_app' AND p.oid::regprocedure::text = ANY(%s))
      )
)
SELECT p.signature, pg_get_userbyid(p.proowner),
       CASE WHEN p.nspname = 'clinic_app' AND p.prokind = 'f'
             AND p.nspowner = 'clinic_owner'::regrole
             AND (SELECT datdba FROM pg_database WHERE datname = current_database())
                 = 'clinic_owner'::regrole
             AND NOT EXISTS (SELECT FROM extension_members e WHERE e.oid = p.oid)
            THEN encode(sha256(convert_to(pg_get_functiondef(p.oid), 'UTF8')), 'hex')
       END,
       ARRAY(SELECT CASE WHEN grantee = 0 THEN 'PUBLIC'
                         ELSE pg_get_userbyid(grantee)::text END
             FROM unnest(p.executors) AS grantee),
       reached.logins
FROM restricted p CROSS JOIN LATERAL (
    SELECT array_agg(DISTINCT family.login) AS logins
    FROM family JOIN pg_roles role USING (oid)
    WHERE role.rolsuper OR 0 = ANY(p.executors) OR family.oid = ANY(p.executors)
) reached
WHERE reached.logins IS NOT NULL
""")
# Every pg_db_role_setting row that can apply to a pinned role in this database:
# role-level (setdatabase = 0), role-in-database, database-wide (setrole = 0)
# and ALTER ROLE ALL. Rows are (role or '', database-scoped, sorted settings).
ROLE_SETTINGS_QUERY: Final = ROLE_FAMILY + sql.SQL("""
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
""")
EXPECTED_ROLE_SETTINGS: Final = [
    ("clinic_agent", False, list(PARAMETER_LOG_SETTINGS)),
    ("clinic_app", False, list(RUNTIME_ROLE_SETTINGS)),
    ("clinic_owner", False, list(PARAMETER_LOG_SETTINGS)),
]
# The session this check opens is itself a fresh runtime session; its effective
# values catch any precedence path the catalog comparison does not model.
EFFECTIVE_SETTINGS_QUERY: Final = (
    "SELECT pg_catalog.current_setting('search_path'), "
    "pg_catalog.current_setting('idle_in_transaction_session_timeout'), "
    "pg_catalog.current_setting('log_error_verbosity'), "
    "pg_catalog.current_setting('log_parameter_max_length'), "
    "pg_catalog.current_setting('log_parameter_max_length_on_error')"
)
EXPECTED_EFFECTIVE_SETTINGS: Final = ("clinic_app, public", "15s", "terse", "0", "0")
FAILURE_MESSAGE: Final = "database posture check failed"
READ_ONLY: Final = "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
CATALOG_PATH: Final = "SET LOCAL search_path = pg_catalog, pg_temp"
ALL_DATABASES: Final = """
SELECT datname, oid FROM pg_catalog.pg_database ORDER BY datname
"""
DATABASE_IDENTITY: Final = """
SELECT session_user, current_user, d.datname, d.oid
FROM pg_database d, pg_roles r
WHERE d.datname = current_database() AND r.rolname = current_user
  AND NOT (r.rolsuper OR r.rolbypassrls OR r.rolcreatedb OR r.rolcreaterole
           OR r.rolreplication OR EXISTS (SELECT FROM pg_auth_members
                                          WHERE member = r.oid OR roleid = r.oid)
           OR EXISTS (SELECT FROM pg_database WHERE datdba = r.oid))
"""


@cache
def _routine_exceptions() -> dict[tuple[str, str, str], set[str]]:
    exceptions = {}
    path = Path(__file__).with_name("routine-exceptions.tsv")
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#"):
            continue
        signature, owner, digest, roles, _reason = line.split("\t")
        exceptions[signature, owner, digest] = set(roles.split(","))
    return exceptions


def _role_authority_safe(connection: psycopg.Connection, logins: list[str]) -> bool:
    if connection.execute(SAFE_ROLE_FAMILY_QUERY, (logins,)).fetchone() != (True,):
        return False
    allowed = _routine_exceptions()
    signatures = [key[0] for key in allowed]
    for signature, owner, digest, grantees, roots in connection.execute(
        RESTRICTED_ROUTINES, (logins, signatures)
    ):
        roles = allowed.get((signature, owner, digest))
        if not roles or not set(grantees) <= roles:
            return False
        # The migration owner deliberately reaches the app and resolver. A
        # runtime login must not acquire an owner-only helper along that path.
        for root in roots:
            audience = "clinic_agent" if root.startswith("clinic_agent_") else root
            if audience != "clinic_owner" and audience not in roles:
                return False
    return True


def _inspect_database_authority(
    primary: psycopg.Connection, database_url: str, password: str, logins: list[str]
) -> bool:
    """Inspect every database with the independent catalog-only login."""
    targets = primary.execute(ALL_DATABASES).fetchall()
    if not any(name == primary.info.dbname for name, _ in targets):
        return False
    for name, oid in targets:
        with psycopg.connect(
            database_url,
            user="clinic_posture",
            password=password,
            dbname=name,
            host=primary.info.host,
            hostaddr=primary.info.hostaddr,
            port=primary.info.port,
            connect_timeout=5,
        ) as peer:
            peer.execute(READ_ONLY)
            peer.execute(CATALOG_PATH)
            if peer.execute(DATABASE_IDENTITY).fetchone() != (
                "clinic_posture",
                "clinic_posture",
                name,
                oid,
            ) or not _role_authority_safe(peer, logins):
                return False
    return True


def _verify_database_posture(database_url: str, test_database_name: str) -> bool:
    try:
        connection_settings = conninfo_to_dict(database_url)
        password = os.environ.get("CLINIC_POSTURE_PASSWORD", "")
        required_settings = (
            connection_settings.get("user") == "clinic_app"
            and bool(password)
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
            connection.execute(READ_ONLY)
            effective = connection.execute(EFFECTIVE_SETTINGS_QUERY).fetchone()
            connection.execute(CATALOG_PATH)
            result = connection.execute(
                POSTURE_QUERY,
                (database_name, test_database_name, test_database_name),
            ).fetchone()
            machines = [
                name for (name,) in connection.execute(MACHINE_LOGINS_QUERY).fetchall()
            ]
            logins = [*PINNED_ROLES, *machines]
            safe_roles = _inspect_database_authority(
                connection, database_url, password, logins
            )
            role_settings = [
                (role, scoped, list(settings))
                for role, scoped, settings in connection.execute(
                    ROLE_SETTINGS_QUERY, (logins,)
                ).fetchall()
            ]
    except (psycopg.Error, ValueError, OSError):
        return False
    return (
        result == (True,)
        and safe_roles
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

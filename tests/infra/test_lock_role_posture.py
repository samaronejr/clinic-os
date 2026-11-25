"""The outbox lock session's role: exact grants, and nothing can signal it."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from config.settings.contracts import require_locks_database_url
from django.core.exceptions import ImproperlyConfigured
from django.db import connection, connections

pytestmark = pytest.mark.django_db(transaction=True)

ROOT = Path(__file__).resolve().parents[2]
LOCK_FUNCTIONS = {
    "audit_append(text,text,inet,text,text,timestamp with time zone,jsonb,bytea)",
    "comms_attempt_scope(uuid)",
    "comms_claim_send_permit(uuid)",
    "comms_close_unsent_attempt(uuid,text,text)",
    "comms_record_send_outcome(uuid,text,text,text)",
}


def test_lock_role_is_an_unprivileged_login_nothing_can_assume() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcanlogin, rolsuper, rolbypassrls, rolcreatedb, "
            "rolcreaterole, rolreplication, rolinherit FROM pg_roles "
            "WHERE rolname='clinic_lock'"
        )
        assert cursor.fetchone() == (True, False, False, False, False, False, False)
        # In no role (no SET ROLE back to clinic_app, no pg_signal_backend or
        # server-file/logging role) and with no member (no runtime session
        # has its privileges, so none may terminate or cancel it).
        cursor.execute(
            "SELECT member::regrole::text, roleid::regrole::text "
            "FROM pg_auth_members WHERE member='clinic_lock'::regrole "
            "OR roleid='clinic_lock'::regrole"
        )
        assert cursor.fetchall() == []
        cursor.execute(
            "SELECT pg_has_role('clinic_app', 'clinic_lock', 'USAGE'), "
            "pg_has_role('clinic_app', 'clinic_lock', 'SET'), "
            "pg_has_role('clinic_lock', 'clinic_app', 'SET'), "
            "pg_has_role('clinic_app', 'pg_signal_backend', 'USAGE')"
        )
        assert cursor.fetchone() == (False, False, False, False)
        cursor.execute(
            "SELECT relname FROM pg_class WHERE relowner='clinic_lock'::regrole "
            "UNION ALL SELECT nspname FROM pg_namespace "
            "WHERE nspowner='clinic_lock'::regrole "
            "UNION ALL SELECT datname FROM pg_database "
            "WHERE datdba='clinic_lock'::regrole "
            "UNION ALL SELECT parname FROM pg_parameter_acl a, "
            "aclexplode(a.paracl) x WHERE x.grantee='clinic_lock'::regrole"
        )
        assert cursor.fetchall() == []


def test_lock_role_grants_are_exactly_the_lock_path() -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.proname || '(' || oidvectortypes(p.proargtypes) || ')' "
            "FROM pg_proc p, aclexplode(p.proacl) x "
            "WHERE x.grantee = 'clinic_lock'::regrole AND x.privilege_type='EXECUTE'"
        )
        granted = {str(row[0]).replace(", ", ",") for row in cursor.fetchall()}
        cursor.execute(
            "SELECT table_name, privilege_type FROM "
            "information_schema.role_table_grants WHERE grantee='clinic_lock' "
            "UNION ALL SELECT table_name || '.' || column_name, privilege_type "
            "FROM information_schema.column_privileges WHERE grantee='clinic_lock'"
        )
        tables = cursor.fetchall()
        cursor.execute(
            "SELECT has_database_privilege('clinic_lock', current_database(), "
            "'CONNECT'), has_database_privilege('clinic_lock', current_database(), "
            "'CREATE'), has_schema_privilege('clinic_lock', 'clinic_app', 'USAGE'), "
            "has_schema_privilege('clinic_lock', 'clinic_app', 'CREATE')"
        )
        database = cursor.fetchone()
    assert granted == LOCK_FUNCTIONS
    assert tables == []
    assert database == (True, False, True, False)


def test_the_test_lock_alias_logs_in_as_clinic_lock() -> None:
    with connections["locks"].cursor() as cursor:
        cursor.execute("SELECT session_user, current_user")
        assert cursor.fetchone() == ("clinic_lock", "clinic_lock")


@pytest.mark.parametrize(
    "environment",
    [
        {},
        {"LOCKS_DATABASE_URL": ""},
        {"LOCKS_DATABASE_URL": "postgresql://clinic_app:x@db:5432/clinic"},
        {"LOCKS_DATABASE_URL": "not a url"},
    ],
)
def test_a_missing_or_runtime_lock_dsn_is_refused(environment: dict[str, str]) -> None:
    with pytest.raises(ImproperlyConfigured, match="must be set and log in as"):
        require_locks_database_url(environment)


def test_a_clinic_lock_dsn_is_accepted() -> None:
    dsn = "postgresql://clinic_lock:x@db:5432/clinic"
    assert require_locks_database_url({"LOCKS_DATABASE_URL": dsn}) == dsn


@pytest.mark.parametrize("module", ["config.settings.base", "config.settings.test"])
def test_startup_fails_closed_without_a_lock_dsn(module: str) -> None:
    # APP_DATABASE_URL is present, so a fallback would have started.
    environment = {
        key: value for key, value in os.environ.items() if key != "LOCKS_DATABASE_URL"
    }
    environment["DJANGO_SETTINGS_MODULE"] = module
    environment.setdefault("APP_DATABASE_URL", "postgresql://clinic_app:x@db:1/c")
    started = subprocess.run(
        [sys.executable, "-c", "import django; django.setup()"],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )
    assert started.returncode != 0
    assert "LOCKS_DATABASE_URL must be set and log in as clinic_lock" in started.stderr

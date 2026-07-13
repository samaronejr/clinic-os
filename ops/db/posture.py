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
            result = connection.execute(
                POSTURE_QUERY,
                (database_name, test_database_name, test_database_name),
            ).fetchone()
    except (psycopg.Error, ValueError):
        return False
    return result == (True,)


def _main() -> int:
    database_url = os.environ.get("APP_DATABASE_URL", "")
    test_database_name = os.environ.get("TEST_DATABASE_NAME", "")
    if not _verify_database_posture(database_url, test_database_name):
        sys.stderr.write(f"{FAILURE_MESSAGE}\n")
        return 1
    sys.stdout.write(
        "database posture: clinic_app runtime; "
        f"clinic_owner NOCREATEDB; {test_database_name} precreated\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())

"""Explicit test-only tampering with sealed templates; always reseal and restore."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg
from django.db import connection
from psycopg import sql

from .world_database import admin_url

if TYPE_CHECKING:
    from collections.abc import Iterator


@contextmanager
def unsealed(database: str) -> Iterator[psycopg.Connection[tuple[object, ...]]]:
    with psycopg.connect(admin_url("postgres"), autocommit=True) as manager:
        manager.execute(
            sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS true").format(
                sql.Identifier(database)
            )
        )
        try:
            with psycopg.connect(admin_url(database), autocommit=True) as admin:
                yield admin
        finally:
            manager.execute(
                sql.SQL("ALTER DATABASE {} ALLOW_CONNECTIONS false").format(
                    sql.Identifier(database)
                )
            )


CATALOG_PLANTS = {
    "column-acl": (
        (
            "GRANT UPDATE(name) ON clinic_app.scheduling_resource TO PUBLIC",
            "REVOKE UPDATE(name) ON clinic_app.scheduling_resource FROM PUBLIC",
        ),
        (
            "GRANT REFERENCES(name) ON clinic_app.scheduling_resource TO clinic_app",
            "REVOKE REFERENCES(name) ON clinic_app.scheduling_resource FROM clinic_app",
        ),
    ),
    "default-acl": (
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
            "GRANT TRIGGER ON TABLES TO PUBLIC",
            "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
            "REVOKE TRIGGER ON TABLES FROM PUBLIC",
        ),
        (
            "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
            "GRANT REFERENCES ON TABLES TO PUBLIC",
            "ALTER DEFAULT PRIVILEGES FOR ROLE clinic_owner IN SCHEMA clinic_app "
            "REVOKE REFERENCES ON TABLES FROM PUBLIC",
        ),
    ),
    "cast": (
        ("CREATE CAST (boolean AS point) WITH INOUT", "DROP CAST (boolean AS point)"),
        ("CREATE CAST (boolean AS circle) WITH INOUT", "DROP CAST (boolean AS circle)"),
    ),
    "operator": (
        (
            "CREATE OPERATOR clinic_app.#@# (LEFTARG=integer,RIGHTARG=integer,"
            "FUNCTION=pg_catalog.int4pl)",
            "DROP OPERATOR clinic_app.#@# (integer,integer)",
        ),
        (
            "CREATE OPERATOR clinic_app.#@> (LEFTARG=integer,RIGHTARG=integer,"
            "FUNCTION=pg_catalog.int4mi)",
            "DROP OPERATOR clinic_app.#@> (integer,integer)",
        ),
    ),
}


def plant(database: str, kind: str, variant: int) -> tuple[str, str]:
    if kind != "sequence":
        return CATALOG_PLANTS[kind][variant]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT quote_ident(n.nspname)||'.'||quote_ident(c.relname),"
            "s.seqincrement,s.seqcache FROM pg_sequence s "
            "JOIN pg_class c ON c.oid=s.seqrelid "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "ORDER BY s.seqrelid LIMIT 1"
        )
        row = cursor.fetchone()
    assert row is not None
    option = "INCREMENT" if variant == 0 else "CACHE"
    return (
        f"ALTER SEQUENCE {row[0]} {option} 7",
        f"ALTER SEQUENCE {row[0]} {option} {row[variant + 1]}",
    )

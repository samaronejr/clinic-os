"""Trusted-owner clock injection on the isolated workflow database only."""

from __future__ import annotations

from contextlib import contextmanager
from typing import TYPE_CHECKING

from django.db import connection
from psycopg import sql

from identity.permission_support import owner_context

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from datetime import datetime
    from uuid import UUID


@contextmanager
def database_clock(
    organization: UUID, now: Callable[[], datetime]
) -> Iterator[Callable[[], None]]:
    """Inject both clock consumers and restore the exact owned function body."""
    with owner_context(organization), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_get_functiondef('clinic_app.workflows_now()'::regprocedure)"
        )
        row = cursor.fetchone()
        assert row is not None
        original: str = row[0]

    def update() -> None:
        with owner_context(organization), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(
                sql.SQL(
                    "CREATE OR REPLACE FUNCTION clinic_app.workflows_now() "
                    "RETURNS timestamptz LANGUAGE sql STABLE SECURITY DEFINER "
                    "SET search_path=pg_catalog,clinic_app,pg_temp AS {}"
                ).format(sql.Literal(f"SELECT '{now().isoformat()}'::timestamptz"))
            )

    try:
        update()
        yield update
    finally:
        with owner_context(organization), connection.cursor() as cursor:
            cursor.execute("SET LOCAL ROLE clinic_resolver")
            cursor.execute(original)

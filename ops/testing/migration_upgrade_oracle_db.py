"""Read fixture values in one snapshot, or in a sweep-owned savepoint."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import TYPE_CHECKING

import psycopg

if TYPE_CHECKING:
    from collections.abc import Iterator

    from psycopg import Connection, Cursor


@contextmanager
def oracle_cursor(
    database: Connection[tuple[object, ...]] | None,
) -> Iterator[tuple[Connection[tuple[object, ...]], Cursor[tuple[object, ...]]]]:
    """Leave an injected transaction under its caller's ownership."""
    if database is not None:
        with database.cursor() as cursor:
            yield database, cursor
        return
    with (
        psycopg.connect(os.environ["UPGRADE_ADMIN_DATABASE_URL"]) as admin,
        admin.cursor() as cursor,
    ):
        cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
        yield admin, cursor

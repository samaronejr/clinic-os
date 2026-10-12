"""Derive datetime clock tokens from the running PostgreSQL executable and parser."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from typing import TYPE_CHECKING

import psycopg
from django.db import connection

from database_urls import database_url_for_name

if TYPE_CHECKING:
    from collections.abc import Iterable


@lru_cache(maxsize=1)
def _words(binary: bytes) -> tuple[str, ...]:
    # PostgreSQL's datetime token table is compiled into its executable. This is
    # a candidate vocabulary, not a hand-kept list of successful input spellings.
    return tuple(
        sorted(
            {word.decode("ascii").lower() for word in re.findall(rb"[A-Za-z]+", binary)}
        )
    )


def clock_tokens_from_candidates(
    db: psycopg.Connection[tuple[object, ...]], candidates: Iterable[str]
) -> frozenset[str]:
    rows = db.execute(
        "SELECT token FROM unnest(%s::text[]) AS token WHERE CASE "
        "WHEN pg_input_is_valid(token,'timestamptz') THEN "
        "token::timestamptz = ANY(ARRAY[now(),current_date::timestamptz,"
        "(current_date-1)::timestamptz,(current_date+1)::timestamptz]) ELSE false END",
        [list(candidates)],
    ).fetchall()
    return frozenset(str(row[0]) for row in rows)


@lru_cache(maxsize=8)
def _server_tokens(database_url: str) -> frozenset[str]:
    # Test-admin access is restricted to this test database. Nothing is installed
    # or changed. Ordinary census execution still runs as the migration owner.
    with psycopg.connect(database_url) as db:
        row = db.execute(
            "SELECT setting || '/postgres' FROM pg_config() WHERE name='BINDIR'"
        ).fetchone()
        assert row is not None
        executable = db.execute("SELECT pg_read_binary_file(%s)", [row[0]]).fetchone()
        assert executable is not None
        assert isinstance(executable[0], bytes)
        return clock_tokens_from_candidates(db, _words(executable[0]))


def special_datetime_tokens() -> frozenset[str]:
    """One parser-validated token vocabulary per immutable server/test session."""
    database_url = database_url_for_name(
        os.environ["TEST_SUPERUSER_DATABASE_URL"], str(connection.settings_dict["NAME"])
    )
    return _server_tokens(database_url)

"""Transfer a column-reveal key without putting it in relational SQL."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import connection
from django.db.backends.utils import CursorDebugWrapper, CursorWrapper
from psycopg import Connection, Cursor

from apps.tenancy.envelope import EnvelopeUnavailableError, _kek

if TYPE_CHECKING:
    from psycopg.abc import Params, Query

_STAGE = (
    "SELECT pg_catalog.length(pg_catalog.set_config('app.column_reveal_kek', %s, true))"
)
_SAFE_BIND_LOGGING = (
    "pg_catalog.current_setting('log_parameter_max_length_on_error') = '0' "
    "AND (pg_catalog.current_setting('log_parameter_max_length') = '0' OR ("
    "pg_catalog.current_setting('log_statement') <> 'all' "
    "AND pg_catalog.current_setting('log_min_duration_statement') = '-1' "
    "AND pg_catalog.current_setting('log_min_duration_sample') = '-1' "
    "AND pg_catalog.current_setting('log_transaction_sample_rate') = '0')) "
    "AND pg_catalog.current_setting('debug_print_plan') = 'off' "
    "AND coalesce(pg_catalog.current_setting('auto_explain.log_min_duration', true), "
    "'-1') = '-1'"
)


class _KeyCursor(Cursor[tuple[object, ...]]):
    """Keep key material out of Django's SQL and debug-bind interfaces."""

    def __init__(self, conn: Connection[tuple[object, ...]], key: str) -> None:
        super().__init__(conn)
        self._key = key

    def execute(
        self,
        query: Query,
        params: Params | None = None,
        *,
        prepare: bool | None = None,
        binary: bool | None = None,
    ) -> _KeyCursor:
        if query != _STAGE or params is not None or prepare or binary:
            raise EnvelopeUnavailableError
        super().execute(query, (self._key,), prepare=False)
        return self


def stage_column_key() -> None:
    """Bind only to a relation-free statement, on this transaction's backend."""
    key = _kek()
    if not connection.in_atomic_block:
        raise EnvelopeUnavailableError
    with connection.cursor() as cursor:
        cursor.execute(f"SELECT {_SAFE_BIND_LOGGING}")
        if cursor.fetchone() != (True,):
            raise EnvelopeUnavailableError
    driver = connection.connection
    if not isinstance(driver, Connection):
        raise EnvelopeUnavailableError
    raw = _KeyCursor(driver, key)
    wrap = CursorDebugWrapper if connection.queries_logged else CursorWrapper
    with wrap(raw, connection) as cursor:
        # Debug captures see only the template; the psycopg Cursor binds $1.
        cursor.execute(_STAGE)

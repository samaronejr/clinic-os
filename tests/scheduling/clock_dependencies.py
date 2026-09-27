"""Obtain SQL-function/view edges from PostgreSQL, never guess relation names."""

from __future__ import annotations

from dataclasses import dataclass

from django.db import DatabaseError, connection, transaction


@dataclass(frozen=True)
class Dependencies:
    functions: frozenset[int] = frozenset()
    relations: frozenset[int] = frozenset()
    unresolved: bool = False


def _edges(oid: int) -> Dependencies:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT refclassid::regclass::text,refobjid FROM pg_depend "
            "WHERE classid='pg_proc'::regclass AND objid=%s "
            "AND refclassid IN ('pg_proc'::regclass,'pg_class'::regclass)",
            [oid],
        )
        rows = cursor.fetchall()
    return Dependencies(
        frozenset(int(ref) for kind, ref in rows if kind == "pg_proc"),
        frozenset(int(ref) for kind, ref in rows if kind == "pg_class"),
    )


def sql_dependencies(oid: int) -> Dependencies:
    """Parse quoted SQL bodies using rolled-back SQL-standard function DDL.

    PostgreSQL does not store quoted-body dependencies. BEGIN ATOMIC asks the
    server to analyse the identical SQL, without executing it; pg_depend supplies
    the resulting edges. Unresolvable bodies are a risky result, never empty edges.
    """
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT p.prosrc,pg_get_function_arguments(p.oid),"
            "pg_get_function_result(p.oid),p.proconfig,p.prosqlbody IS NOT NULL, "
            "current_setting('search_path') "
            "FROM pg_proc p WHERE p.oid=%s",
            [oid],
        )
        row = cursor.fetchone()
    assert row is not None
    body, arguments, result, settings, parsed, current_path = row
    if parsed:
        return _edges(oid)
    name = f"clock_dependency_{oid}"
    search_path = next(
        (
            value.split("=", 1)[1]
            for value in settings or ()
            if value.startswith("search_path=")
        ),
        str(current_path),
    )
    try:
        with transaction.atomic():
            try:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT set_config('search_path',%s,true)", [search_path]
                    )
                    cursor.execute(
                        "CREATE TEMP TABLE clock_dependency_anchor (unused integer) "
                        "ON COMMIT DROP"
                    )
                    definition = (
                        f"CREATE FUNCTION pg_temp.{name}({arguments}) RETURNS {result} "
                        f"LANGUAGE sql SET search_path TO {search_path} "
                        f"BEGIN ATOMIC {str(body).rstrip().rstrip(';')}\n; END"
                    )
                    cursor.execute(definition)
                    cursor.execute(
                        "SELECT oid FROM pg_proc "
                        "WHERE pronamespace=pg_my_temp_schema() "
                        "AND proname=%s",
                        [name],
                    )
                    created = cursor.fetchone()
                    assert created is not None
                return _edges(int(created[0]))
            finally:
                transaction.set_rollback(True)
    except DatabaseError:
        # This explicit unresolved result is surfaced as a potential clock read.
        # The caller cannot pass the inventory by silently discarding a parse error.
        return Dependencies(unresolved=True)

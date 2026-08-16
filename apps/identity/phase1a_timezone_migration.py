"""Frozen FORCE-RLS data operations for identity migration 0005."""

from __future__ import annotations

from typing import TYPE_CHECKING, Final, Never
from uuid import UUID

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.backends.utils import CursorWrapper
    from django.db.migrations.state import StateApps

BACKFILL_TIMEZONE: Final = "America/Sao_Paulo"
CONTEXT_ERROR: Final = "clinic timezone migration context unavailable"
ENUMERATION_ERROR: Final = "clinic timezone enumeration failed"
UPDATE_ERROR: Final = "clinic timezone backfill failed"
CONTEXT_FIELD_COUNT: Final = 3
PAIR_FIELD_COUNT: Final = 2
SAVEPOINT: Final = "phase1a_clinic_timezone"


def _fail_enumeration() -> Never:
    raise RuntimeError(ENUMERATION_ERROR)


def _saved_context(cursor: CursorWrapper) -> tuple[str, str | None, str | None]:
    cursor.execute(
        "SELECT current_setting('role'), "
        "current_setting('app.current_tenant', true), "
        "current_setting('app.current_user_id', true)"
    )
    row = cursor.fetchone()
    if row is None or not isinstance(row, tuple) or len(row) != CONTEXT_FIELD_COUNT:
        raise RuntimeError(CONTEXT_ERROR)
    role, tenant, user = row
    if (
        not isinstance(role, str)
        or (tenant is not None and not isinstance(tenant, str))
        or (user is not None and not isinstance(user, str))
    ):
        raise RuntimeError(CONTEXT_ERROR)
    return role, tenant, user


def _restore_context(
    cursor: CursorWrapper,
    schema_editor: BaseDatabaseSchemaEditor,
    saved: tuple[str, str | None, str | None],
) -> None:
    role, tenant, user = saved
    cursor.execute("RESET ROLE")
    if role != "none":
        cursor.execute(f"SET ROLE {schema_editor.quote_name(role)}")
    for setting, value in (
        ("app.current_tenant", tenant),
        ("app.current_user_id", user),
    ):
        if value is None:
            cursor.execute(f"RESET {setting}")
        else:
            cursor.execute(
                "SELECT pg_catalog.set_config(%s, %s, true)",
                [setting, value],
            )


def _clinic_pairs(rows: list[tuple[object, ...]]) -> tuple[tuple[UUID, UUID], ...]:
    pairs: list[tuple[UUID, UUID]] = []
    for row in rows:
        if (
            not isinstance(row, tuple)
            or len(row) != PAIR_FIELD_COUNT
            or not isinstance(row[0], UUID)
            or not isinstance(row[1], UUID)
        ):
            _fail_enumeration()
        pairs.append((row[0], row[1]))
    if pairs != sorted(set(pairs)):
        _fail_enumeration()
    return tuple(pairs)


def _enumerate_clinics(
    cursor: CursorWrapper,
    schema_editor: BaseDatabaseSchemaEditor,
    saved: tuple[str, str | None, str | None],
) -> tuple[tuple[UUID, UUID], ...]:
    cursor.execute("SET LOCAL ROLE clinic_resolver")
    cursor.execute(
        "SELECT organization_id, id "
        "FROM clinic_app.identity_clinic ORDER BY organization_id, id"
    )
    rows = cursor.fetchall()
    if not isinstance(rows, list):
        _fail_enumeration()
    _restore_context(cursor, schema_editor, saved)
    return _clinic_pairs(rows)


def _set_timezone(
    schema_editor: BaseDatabaseSchemaEditor,
    timezone_key: str | None,
) -> None:
    with schema_editor.connection.cursor() as cursor:
        saved = _saved_context(cursor)
        cursor.execute(f"SAVEPOINT {SAVEPOINT}")
        succeeded = False
        try:
            pairs = _enumerate_clinics(cursor, schema_editor, saved)
            for organization_id, clinic_id in pairs:
                cursor.execute(
                    "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                    [str(organization_id)],
                )
                cursor.execute(
                    "UPDATE clinic_app.identity_clinic SET timezone = %s "
                    "WHERE organization_id = %s AND id = %s RETURNING id",
                    [timezone_key, organization_id, clinic_id],
                )
                if cursor.fetchone() != (clinic_id,) or cursor.rowcount != 1:
                    raise RuntimeError(UPDATE_ERROR)
                _restore_context(cursor, schema_editor, saved)
            succeeded = True
        finally:
            if not succeeded:
                cursor.execute(f"ROLLBACK TO SAVEPOINT {SAVEPOINT}")
            _restore_context(cursor, schema_editor, saved)
            cursor.execute(f"RELEASE SAVEPOINT {SAVEPOINT}")


def backfill_clinic_timezones(
    _apps: StateApps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Backfill each FORCE-RLS tenant with the foundation synthetic zone."""
    _set_timezone(schema_editor, BACKFILL_TIMEZONE)


def clear_clinic_timezones(
    _apps: StateApps,
    schema_editor: BaseDatabaseSchemaEditor,
) -> None:
    """Clear the transient column before reversing its schema operation."""
    _set_timezone(schema_editor, None)

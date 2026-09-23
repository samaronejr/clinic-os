"""Shared helpers for resumable plaintext-to-envelope column migrations.

Every protected column migrates through the same shape: the plaintext column
is renamed to ``<name>_legacy``, a ``bytea`` envelope column takes the
original name, a backfill encrypts each legacy value through
``clinic_app.protected_encrypt`` (verifying the round trip in-database), and
the legacy column is dropped. All database operations are idempotent so an
interrupted migration resumes where it stopped; the migration is
irreversible because rollback must never reactivate plaintext storage —
recovery happens on the owned restored target instead.

Isolation contract: each table pass runs inside one transaction. Row-level
security and user triggers are disabled for the migration's own scan only
inside that transaction — catalog changes are transactional, so concurrent
sessions never observe the table without RLS, and an interruption rolls the
toggles back with the data. Triggers are disabled because the backfill must
not invent clinical transitions (revision bumps) or fabricate receipt rows;
foreign-key constraint triggers are not user triggers and stay enforced.

Deferred constraint triggers (the ORM's ``INITIALLY DEFERRED`` foreign keys)
queue an event on the scanned table for every row the backfill updates, and
PostgreSQL refuses ``ALTER TABLE`` while that table has pending trigger
events. Each pass therefore runs ``SET CONSTRAINTS ALL IMMEDIATE`` first, so
every deferred check fires at the end of its own statement — still inside
the same transaction, still enforced — leaving the queue empty when the
trigger toggles are restored.

A legacy value the encoder maps to ``None`` (the field contract's
empty-to-NULL encoding, e.g. ``reopen_reason = ''``) must not stay pending
forever: the completeness predicate treats a legacy value as unmigrated
only when its text form is non-empty — ``''`` is the only value the
contract encodes to NULL, and bytea/jsonb/date legacy values never render
as ``''`` — so an empty string seals as migrated while every other
non-NULL value still blocks completion.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.db import transaction
from django.db.migrations.exceptions import IrreversibleError

if TYPE_CHECKING:
    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.backends.utils import CursorWrapper

# One plan is ``(new_column, purpose, legacy_columns, extra_columns, encode,
# digest_column)``; ``encode`` maps one read row to its plaintext bytes.
ProtectedRow = dict[str, Any]
ProtectedEncoder = Callable[[ProtectedRow], bytes | None]
ProtectedPlan = tuple[
    str, str, tuple[str, ...], tuple[str, ...], ProtectedEncoder, str | None
]

_CLINIC_SCHEMA = "clinic_app"
_IRREVERSIBLE_MESSAGE = (
    "protected-field migration is irreversible; recover on the owned "
    "restored target instead of reactivating plaintext storage"
)


def _column_exists_sql(table: str, column: str) -> str:
    return f"""
        SELECT 1 FROM pg_catalog.pg_attribute AS attribute
        JOIN pg_catalog.pg_class AS relation
          ON relation.oid = attribute.attrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = '{_CLINIC_SCHEMA}'
          AND relation.relname = '{table}'
          AND attribute.attname = '{column}'
          AND attribute.attnum > 0 AND NOT attribute.attisdropped
    """  # noqa: S608 - catalog lookup built from migration literals


def rename_to_legacy_sql(table: str, column: str) -> str:
    """Rename the plaintext column once; safe to re-run after a failure."""
    return f"""
DO $migration$
BEGIN
    IF EXISTS ({_column_exists_sql(table, column)})
       AND NOT EXISTS ({_column_exists_sql(table, column + "_legacy")}) THEN
        ALTER TABLE {_CLINIC_SCHEMA}.{table}
            RENAME COLUMN {column} TO {column}_legacy;
    END IF;
END
$migration$;
ALTER TABLE {_CLINIC_SCHEMA}.{table}
    ADD COLUMN IF NOT EXISTS {column} pg_catalog.bytea;
"""


def drop_legacy_sql(table: str, column: str, *, required: bool) -> str:
    """Drop the plaintext copy and enforce presence when required."""
    statement = (
        f"ALTER TABLE {_CLINIC_SCHEMA}.{table} DROP COLUMN IF EXISTS {column}_legacy;"
    )
    if required:
        statement += (
            f"\nALTER TABLE {_CLINIC_SCHEMA}.{table} "
            f"ALTER COLUMN {column} SET NOT NULL;"
        )
    return statement


def drop_constraint_sql(table: str, name: str) -> str:
    """Drop one constraint only when it still exists."""
    return f"ALTER TABLE {_CLINIC_SCHEMA}.{table} DROP CONSTRAINT IF EXISTS {name};"


def add_constraint_sql(table: str, name: str, clause: str) -> str:
    """Add one constraint only when it is not already present."""
    return f"""
DO $migration$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_catalog.pg_constraint AS constraint_row
        JOIN pg_catalog.pg_class AS relation
          ON relation.oid = constraint_row.conrelid
        JOIN pg_catalog.pg_namespace AS namespace
          ON namespace.oid = relation.relnamespace
        WHERE namespace.nspname = '{_CLINIC_SCHEMA}'
          AND relation.relname = '{table}'
          AND constraint_row.conname = '{name}'
    ) THEN
        ALTER TABLE {_CLINIC_SCHEMA}.{table}
            ADD CONSTRAINT {name} CHECK ({clause});
    END IF;
END
$migration$;
"""  # noqa: S608 - DDL built from migration literals, never user input


def _has_column(cursor: CursorWrapper, table: str, column: str) -> bool:
    cursor.execute(_column_exists_sql(table, column))
    return cursor.fetchone() is not None


def _pending_predicate(plans: tuple[ProtectedPlan, ...]) -> str:
    """Build the SQL predicate matching rows that still hold plaintext.

    A row is pending while its envelope column is NULL and any of the
    plan's legacy columns holds a value the encoder would encrypt. The
    field contract encodes the empty string to NULL, so a legacy value
    counts as unmigrated only when its text form is non-empty; bytea,
    jsonb and date values never render as ``''``, so the clause reduces to
    ``IS NOT NULL`` for them.
    """
    return " OR ".join(
        f"{new_column} IS NULL AND ("
        + " OR ".join(
            f"({legacy} IS NOT NULL AND {legacy}::pg_catalog.text <> '')"
            for legacy in legacy_names
        )
        + ")"
        for new_column, _, legacy_names, _, _, _ in plans
    )


def _open_table(cursor: CursorWrapper, table: str) -> None:
    """Suspend RLS and user triggers for this transaction's own scan.

    Both catalog changes are transactional: concurrent sessions keep
    evaluating the committed RLS policies and triggers, and a rollback
    restores them together with the data. ``DISABLE TRIGGER USER`` leaves
    internally generated constraint triggers (foreign keys) enforced; the
    owner-side scan needs no tenant GUC to see rows, so the backfill reads
    every organization while runtime roles remain fully isolated.
    """
    cursor.execute(f"ALTER TABLE {_CLINIC_SCHEMA}.{table} DISABLE ROW LEVEL SECURITY")
    cursor.execute(f"ALTER TABLE {_CLINIC_SCHEMA}.{table} DISABLE TRIGGER USER")


def _close_table(cursor: CursorWrapper, table: str) -> None:
    """Restore ENABLE + FORCE RLS and user triggers before commit."""
    cursor.execute(f"ALTER TABLE {_CLINIC_SCHEMA}.{table} ENABLE TRIGGER USER")
    cursor.execute(f"ALTER TABLE {_CLINIC_SCHEMA}.{table} ENABLE ROW LEVEL SECURITY")
    cursor.execute(f"ALTER TABLE {_CLINIC_SCHEMA}.{table} FORCE ROW LEVEL SECURITY")


def encrypt_backfill(
    *,
    table: str,
    pk_column: str,
    plans: tuple[ProtectedPlan, ...],
) -> Callable[[Apps, BaseDatabaseSchemaEditor], None]:
    """Return the RunPython callable encrypting legacy columns per tenant.

    ``plans`` holds ``(new_column, purpose, legacy_columns, extra_columns,
    encode, digest_column)`` where ``encode`` maps the row dict (legacy plus
    extra columns) to plaintext bytes or ``None`` (which leaves the envelope
    column NULL) and ``digest_column`` optionally receives the plaintext
    SHA-256 in the same UPDATE. Rows whose new column is populated are
    skipped, so a failed run resumes exactly where it stopped. Every written
    envelope is decrypted in-database and compared to the plaintext before
    the batch commits; a mismatch aborts the migration, never the bytes.

    The whole table pass is one transaction: RLS and user triggers are
    suspended only inside it, so the runtime role never observes an
    unprotected table and an interruption leaves no committed open window.
    """

    def run(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        from apps.core.secrets import secret_store  # noqa: PLC0415

        del apps
        connection = schema_editor.connection
        legacy_columns = sorted(
            {legacy for _, _, legacy_names, _, _, _ in plans for legacy in legacy_names}
        )
        read_columns = sorted(
            {
                column
                for _, _, legacy_names, extra_names, _, _ in plans
                for column in (*legacy_names, *extra_names)
            }
        )
        with connection.cursor() as cursor:
            if not all(_has_column(cursor, table, column) for column in legacy_columns):
                return
        with transaction.atomic(using=connection.alias):
            with connection.cursor() as cursor:
                # Deferred foreign-key triggers queue an event on this table
                # for every updated row; firing them per statement keeps the
                # queue empty so the closing ALTER TABLE is legal.
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                _open_table(cursor, table)
            try:
                with connection.cursor() as cursor:
                    pending = _pending_predicate(plans)
                    cursor.execute(
                        f"SELECT DISTINCT organization_id "  # noqa: S608
                        f"FROM {_CLINIC_SCHEMA}.{table} WHERE {pending}"
                    )
                    organizations = [row[0] for row in cursor.fetchall()]
                if organizations:
                    kek = secret_store().get_secret("tenant-kek")
                    for organization_id in organizations:
                        with connection.cursor() as cursor:
                            cursor.execute(
                                "SELECT pg_catalog.set_config("
                                "'app.current_tenant', %s, true)",
                                [str(organization_id)],
                            )
                            cursor.execute(
                                f"SELECT {pk_column}, "  # noqa: S608
                                + ", ".join(read_columns)
                                + f" FROM {_CLINIC_SCHEMA}.{table}"
                                f" WHERE organization_id = %s AND ({pending})"
                                f" ORDER BY {pk_column}",
                                [str(organization_id)],
                            )
                            rows = cursor.fetchall()
                            for row in rows:
                                record = dict(
                                    zip(
                                        (pk_column, *read_columns),
                                        row,
                                        strict=True,
                                    )
                                )
                                for (
                                    new_column,
                                    purpose,
                                    _legacy_names,
                                    _extra_names,
                                    encode,
                                    digest_column,
                                ) in plans:
                                    plaintext = encode(record)
                                    if plaintext is None:
                                        continue
                                    digest_assignment = (
                                        ""
                                        if digest_column is None
                                        else (
                                            f", {digest_column} = "
                                            "pg_catalog.encode("
                                            "clinic_app.digest(%s, 'sha256'), "
                                            "'hex')"
                                        )
                                    )
                                    params: list[Any] = [
                                        kek,
                                        purpose,
                                        plaintext,
                                    ]
                                    if digest_column is not None:
                                        params.append(plaintext)
                                    params.append(record[pk_column])
                                    cursor.execute(
                                        f"UPDATE {_CLINIC_SCHEMA}.{table} "  # noqa: S608
                                        f"SET {new_column} = "
                                        "clinic_app.protected_encrypt("
                                        "%s, %s, %s)"
                                        f"{digest_assignment} "
                                        f"WHERE {pk_column} = %s "
                                        f"AND {new_column} IS NULL",
                                        params,
                                    )
                                    cursor.execute(
                                        "SELECT clinic_app.protected_decrypt("  # noqa: S608
                                        f"%s, %s, {new_column}) = %s "
                                        f"FROM {_CLINIC_SCHEMA}.{table} "
                                        f"WHERE {pk_column} = %s",
                                        [
                                            kek,
                                            purpose,
                                            plaintext,
                                            record[pk_column],
                                        ],
                                    )
                                    verified = cursor.fetchone()
                                    if verified is None or verified[0] is not True:
                                        message = (
                                            f"{table}.{new_column} backfill "
                                            "verification failed"
                                        )
                                        raise RuntimeError(message)
            finally:
                with connection.cursor() as cursor:
                    _close_table(cursor, table)

    return run


def require_backfill_complete(
    *,
    table: str,
    plans: tuple[ProtectedPlan, ...],
) -> Callable[[Apps, BaseDatabaseSchemaEditor], None]:
    """Seal the migration: reject any row still holding unmigrated data.

    The scan runs inside one transaction with RLS suspended only for its
    duration, so the runtime role never observes an unprotected table.
    """

    def run(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
        del apps
        connection = schema_editor.connection
        legacy_columns = sorted(
            {legacy for _, _, legacy_names, _, _, _ in plans for legacy in legacy_names}
        )
        with connection.cursor() as cursor:
            if not all(_has_column(cursor, table, column) for column in legacy_columns):
                return
        with transaction.atomic(using=connection.alias):
            with connection.cursor() as cursor:
                cursor.execute("SET CONSTRAINTS ALL IMMEDIATE")
                _open_table(cursor, table)
            try:
                with connection.cursor() as cursor:
                    pending = _pending_predicate(plans)
                    cursor.execute(
                        f"SELECT EXISTS (SELECT 1 FROM {_CLINIC_SCHEMA}.{table} "  # noqa: S608
                        f"WHERE {pending})"
                    )
                    row = cursor.fetchone()
                if row is not None and row[0]:
                    message = f"{table} still holds plaintext legacy values"
                    raise RuntimeError(message)
            finally:
                with connection.cursor() as cursor:
                    _close_table(cursor, table)

    return run


def irreversible(apps: Apps, schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Reverse is forbidden: rollback must never restore plaintext columns."""
    del apps, schema_editor
    raise IrreversibleError(_IRREVERSIBLE_MESSAGE)

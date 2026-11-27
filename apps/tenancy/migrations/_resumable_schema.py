"""Record protected migration steps in the transactions that publish them."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy
from typing import TYPE_CHECKING, Final, Never

from django.db import DatabaseError, migrations, router, transaction
from django.db.migrations.exceptions import IrreversibleError

if TYPE_CHECKING:
    from collections.abc import Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import ProjectState

type MigrationKey = tuple[str, str]
type Progress = dict[MigrationKey, tuple[int, ...]]
type Cut = tuple[MigrationKey, int]
_verified_resume: Final[ContextVar[tuple[BaseDatabaseWrapper, Cut] | None]] = (
    ContextVar("verified_migration_resume", default=None)
)


class MigrationRecoveryError(RuntimeError):
    """A protected forward migration cannot be safely continued."""


def refuse(reason: str) -> Never:
    """Use a stable diagnostic without dumping schemas or user data."""
    msg = f"Migration recovery refused: {reason}; restore or rebuild is required"
    raise MigrationRecoveryError(msg)


def progress(db: BaseDatabaseWrapper) -> Progress:
    """Read the owner-only journal; never create or repair it on retry."""
    with db.cursor() as cursor:
        cursor.execute("SELECT to_regclass('clinic_migration.progress')")
        row = cursor.fetchone()
        if row is None or row[0] is None:
            refuse("missing clinic_migration.progress (check administrator bootstrap)")
        try:
            cursor.execute(
                "SELECT app, name, step FROM clinic_migration.progress "
                "ORDER BY app, name, step"
            )
        except DatabaseError:
            refuse("unreadable clinic_migration.progress")
        grouped: dict[MigrationKey, list[int]] = {}
        for app, name, step in cursor.fetchall():
            grouped.setdefault((app, name), []).append(step)
    return {key: tuple(steps) for key, steps in grouped.items()}


def prefix(key: MigrationKey, steps: tuple[int, ...], length: int) -> int:
    """Reject holes, duplicate ordinals and receipts past the declared plan."""
    if len(steps) > length or steps != tuple(range(1, len(steps) + 1)):
        refuse(f"{'.'.join(key)} has an invalid progress prefix")
    return len(steps)


def flush_deferred(schema_editor: BaseDatabaseSchemaEditor) -> None:
    """Run generated FKs and indexes before the transaction records success."""
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement, None)
    schema_editor.deferred_sql.clear()


def record_step(db: BaseDatabaseWrapper, key: MigrationKey, step: int) -> None:
    """Record a successful effect in the running step transaction."""
    with db.cursor() as cursor:
        cursor.execute(
            "INSERT INTO clinic_migration.progress (app, name, step) "
            "VALUES (%s, %s, %s)",
            (*key, step),
        )


@contextmanager
def allow_verified_resume(db: BaseDatabaseWrapper, cut: Cut | None) -> Iterator[None]:
    """Bind the verified entry receipt to this execution and database."""
    token = _verified_resume.set(None if cut is None else (db, cut))
    try:
        yield
    finally:
        _verified_resume.reset(token)


class GuardedMigration(migrations.Migration):
    """Keep each declared operation and its progress receipt in one commit."""

    atomic = False

    def apply(
        self,
        project_state: ProjectState,
        schema_editor: BaseDatabaseSchemaEditor,
        collect_sql: bool = False,
    ) -> ProjectState:
        """Replay state for every step; execute effects only for the suffix."""
        if collect_sql or schema_editor.collect_sql:
            return super().apply(project_state, schema_editor, collect_sql)
        db = schema_editor.connection
        key = (self.app_label, self.name)
        if not db.get_autocommit():
            refuse(f"{self} needs an autocommit connection at plan entry")
        length = len(self.operations)
        done = prefix(key, progress(db).get(key, ()), length)
        if done and _verified_resume.get() != (db, (key, done)):
            refuse(f"{self} must resume with the approved all-leaves migrate command")
        for step, operation in enumerate(self.operations, start=1):
            old_state = project_state.clone()
            operation.state_forwards(self.app_label, project_state)
            if step > done:
                with transaction.atomic(using=db.alias):
                    operation.database_forwards(
                        self.app_label, schema_editor, old_state, project_state
                    )
                    flush_deferred(schema_editor)
                    record_step(db, key, step)
        if prefix(key, progress(db).get(key, ()), length) != length:
            refuse(f"{self} did not record every step")
        return project_state

    def unapply(
        self,
        project_state: ProjectState,
        schema_editor: BaseDatabaseSchemaEditor,
        collect_sql: bool = False,
    ) -> ProjectState:
        """Refuse before any operation can reverse a protected effect."""
        del project_state, schema_editor, collect_sql
        msg = f"{self} is irreversible; rollback requires restore"
        raise IrreversibleError(msg)


class AtomicSchemaMigration(migrations.Migration):
    """Publish an additive schema and Django's recorder in one transaction."""

    atomic = True

    def apply(
        self,
        project_state: ProjectState,
        schema_editor: BaseDatabaseSchemaEditor,
        collect_sql: bool = False,
    ) -> ProjectState:
        """Drain deferred SQL while Django's atomic editor is still open."""
        state = super().apply(project_state, schema_editor, collect_sql)
        flush_deferred(schema_editor)
        return state


class TransactionScript(migrations.RunSQL):
    """Use Django's step or migration transaction for a historical SQL script."""

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Keep the serialized bytes without sending an inner BEGIN/COMMIT."""
        if schema_editor.collect_sql:
            super().database_forwards(app_label, schema_editor, from_state, to_state)
            return
        script = self.sql
        if not (
            isinstance(script, str)
            and script.startswith("BEGIN;")
            and script.endswith("COMMIT;")
            and schema_editor.connection.in_atomic_block
        ):
            if router.allow_migrate(
                schema_editor.connection.alias, app_label, **self.hints
            ):
                refuse("historical SQL needs its declared outer transaction")
            return
        # Keep the serialized SQL intact. Django forwards an explicit
        # (statement, params) pair in one call without splitting the script.
        step = copy(self)
        step.sql = [(script[len("BEGIN;") : -len("COMMIT;")], None)]
        migrations.RunSQL.database_forwards(
            step, app_label, schema_editor, from_state, to_state
        )

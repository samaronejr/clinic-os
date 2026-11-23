"""Commit and replay a protected schema unit without touching Django's recorder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations

from apps.tenancy.migration_operations import SeparateDatabaseAndState

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState


def relation_exists(editor: BaseDatabaseSchemaEditor, name: str) -> bool:
    """Check a fixed, migration-owned relation in the application schema."""
    with editor.connection.cursor() as cursor:
        cursor.execute("SELECT pg_catalog.to_regclass(%s)", [f"clinic_app.{name}"])
        row = cursor.fetchone()
    return row is not None and row[0] is not None


class CheckpointedSchema(migrations.SeparateDatabaseAndState):
    """Publish the operations and their deferred SQL in one restartable unit.

    ``receipt`` is an index created by Django's deferred DDL in this unit.
    Checking just for a new table would mistake an older, partially applied
    schema for a complete unit. The selected index must follow any other
    deferred DDL needed by earlier operations in the unit.
    """

    atomic = True
    reversible = False

    def __init__(
        self,
        *,
        receipt: str,
        database_operations: Sequence[Operation],
        state_operations: Sequence[Operation],
    ) -> None:
        self.receipt = receipt
        super().__init__(
            database_operations=list(database_operations),
            state_operations=list(state_operations),
        )

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        """Keep the catalog checkpoint when a migration is serialized."""
        return (
            type(self).__name__,
            [],
            {
                "receipt": self.receipt,
                "database_operations": self.database_operations,
                "state_operations": self.state_operations,
            },
        )

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Leave nothing on the outer editor's commit-after-operation queue."""
        if not schema_editor.collect_sql and relation_exists(
            schema_editor, self.receipt
        ):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)
        for statement in schema_editor.deferred_sql:
            schema_editor.execute(statement, None)
        schema_editor.deferred_sql.clear()
        if not schema_editor.collect_sql and not relation_exists(
            schema_editor, self.receipt
        ):
            msg = f"Protected schema unit did not produce {self.receipt}"
            raise RuntimeError(msg)


class ResumableSchema(CheckpointedSchema, SeparateDatabaseAndState):
    """Group a linear sequence without changing its model state or SQL."""

    def __init__(self, receipt: str, operations: Sequence[Operation]) -> None:
        super().__init__(
            receipt=receipt,
            database_operations=operations,
            state_operations=operations,
        )

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        """Serialize the shared operation list only once."""
        return (
            type(self).__name__,
            [],
            {"receipt": self.receipt, "operations": self.database_operations},
        )


class ResumableRunSQL(migrations.RunSQL):
    """Replay-safe wrapper for an existing script that owns BEGIN and COMMIT."""

    def __init__(self, sql: str, *, table: str, trigger: str) -> None:
        self.table = table
        self.trigger = trigger
        self.script = sql
        super().__init__(sql)

    def deconstruct(self) -> tuple[str, list[str], dict[str, str]]:
        """Preserve the checkpoint along with the original SQL."""
        return (
            type(self).__name__,
            [],
            {
                "sql": self.script,
                "table": self.table,
                "trigger": self.trigger,
            },
        )

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Skip only if the script's transaction published its new trigger."""
        if not schema_editor.collect_sql:
            with schema_editor.connection.cursor() as cursor:
                cursor.execute(
                    "SELECT 1 FROM pg_catalog.pg_trigger "
                    "WHERE tgrelid = pg_catalog.to_regclass(%s) AND tgname = %s",
                    [f"clinic_app.{self.table}", self.trigger],
                )
                if cursor.fetchone() is not None:
                    return
        super().database_forwards(app_label, schema_editor, from_state, to_state)


class AtomicRunSQL(migrations.RunSQL):
    """Run a repeatable script as one unit, including its SET LOCAL clauses."""

    atomic = True

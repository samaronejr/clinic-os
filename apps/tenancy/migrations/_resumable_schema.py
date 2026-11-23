"""Commit and replay a protected schema unit without touching Django's recorder."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations

from apps.tenancy.migration_operations import SeparateDatabaseAndState
from apps.tenancy.migrations._resume_catalog import (
    checkpoint_applied,
    require_checkpoint,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState


class CheckpointedSchema(migrations.SeparateDatabaseAndState):
    """Publish the operations and their deferred SQL in one restartable unit.

    ``receipt`` identifies this unit's frozen catalog contract. Its historical
    index name is not proof of completion on its own.
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
        if checkpoint_applied(schema_editor, self.receipt):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)
        for statement in schema_editor.deferred_sql:
            schema_editor.execute(statement, None)
        schema_editor.deferred_sql.clear()
        require_checkpoint(schema_editor, self.receipt)


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
        """Skip only if the whole script's catalog matches its contract."""
        if checkpoint_applied(schema_editor, self.trigger):
            return
        super().database_forwards(app_label, schema_editor, from_state, to_state)
        require_checkpoint(schema_editor, self.trigger)


class AtomicRunSQL(migrations.RunSQL):
    """Publish a script as one unit, refusing conflicting preexisting DDL."""

    atomic = True

    def __init__(
        self, sql: str, *, receipt: str, state_operations: Sequence[Operation]
    ) -> None:
        self.receipt = receipt
        super().__init__(sql, state_operations=list(state_operations))

    def deconstruct(self) -> tuple[str, list[object], dict[str, object]]:
        """Preserve the catalog contract when serializing the operation."""
        name, args, kwargs = super().deconstruct()
        return name, args, {**kwargs, "receipt": self.receipt}

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Run the script only from the exact predecessor catalog."""
        if not checkpoint_applied(schema_editor, self.receipt):
            super().database_forwards(app_label, schema_editor, from_state, to_state)
            require_checkpoint(schema_editor, self.receipt)

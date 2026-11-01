"""Frozen state batching for the additive Wave C schema operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations

if TYPE_CHECKING:
    from collections.abc import Sequence

    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState

_MODEL_OPERATIONS = (
    migrations.AddField,
    migrations.AlterField,
    migrations.AddConstraint,
    migrations.RemoveConstraint,
    migrations.AddIndex,
)


class SchemaStateBatch(migrations.SeparateDatabaseAndState):
    """Render changed models once while retaining original database operations.

    These batches create plain models or change metadata and non-key scalar
    fields on existing models. Added relationships belong to models created in
    the same batch. Existing relationships and key fields must remain unchanged.
    Historical ORM callbacks clear the delayed registry, as they do for the
    existing tenant constraint operations.

    Database operations still run one at a time against their own intermediate
    states. Each state re-renders only the model its operation changed, so the
    schema editor receives the same model shapes without Django rebuilding the
    whole related tenant graph for every wrapped operation.
    """

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Apply the complete state batch before refreshing changed models."""
        _apply_and_render_changed(app_label, self.state_operations, state)

    def database_forwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Run each database operation between targeted intermediate states."""
        for operation in self.database_operations:
            to_state = _following_state(app_label, operation, from_state)
            operation.database_forwards(app_label, schema_editor, from_state, to_state)
            from_state = to_state

    def database_backwards(
        self,
        app_label: str,
        schema_editor: BaseDatabaseSchemaEditor,
        from_state: ProjectState,
        to_state: ProjectState,
    ) -> None:
        """Reverse each database operation between targeted intermediate states."""
        to_states = {}
        for operation in self.database_operations:
            to_states[operation] = to_state
            to_state = _following_state(app_label, operation, to_state)
        for operation in reversed(self.database_operations):
            from_state = to_state
            to_state = to_states[operation]
            operation.database_backwards(app_label, schema_editor, from_state, to_state)


def _following_state(
    app_label: str, operation: Operation, state: ProjectState
) -> ProjectState:
    """Return the state after one database operation, rendering only its model."""
    following = state.clone()
    if isinstance(operation, migrations.RunSQL) and not operation.state_operations:
        return following
    _apply_and_render_changed(app_label, [operation], following)
    return following


def _apply_and_render_changed(
    app_label: str, operations: Sequence[Operation], state: ProjectState
) -> None:
    """Apply state changes without a registry, then render only changed models."""
    registry = state.__dict__.pop("apps", None)
    for operation in operations:
        operation.state_forwards(app_label, state)
    if registry is None:
        return
    changed = {_changed_model(app_label, operation) for operation in operations}
    state.apps = registry
    with registry.bulk_update():
        for key in changed:
            registry.unregister_model(*key)
    registry.render_multiple([state.models[key] for key in sorted(changed)])
    state.is_delayed = True


def _changed_model(app_label: str, operation: Operation) -> tuple[str, str]:
    if isinstance(operation, migrations.CreateModel):
        return app_label, operation.name_lower
    if isinstance(operation, _MODEL_OPERATIONS):
        return app_label, operation.model_name_lower
    msg = "Unsupported Wave C state operation"
    raise TypeError(msg)

"""Frozen state batching for the additive Wave C schema operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations

if TYPE_CHECKING:
    from django.db.migrations.state import ProjectState


class SchemaStateBatch(migrations.SeparateDatabaseAndState):
    """Render changed models once while retaining original database operations.

    These batches create plain models or change metadata and non-key scalar
    fields on existing models. Added relationships belong to models created in
    the same batch. Existing relationships and key fields must remain unchanged.
    Historical ORM callbacks clear the delayed registry, as they do for the
    existing tenant constraint operations.
    """

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Apply the complete state batch before refreshing changed models."""
        registry = state.__dict__.pop("apps", None)
        super().state_forwards(app_label, state)
        if registry is None:
            return
        changed = set()
        for operation in self.state_operations:
            if isinstance(operation, migrations.CreateModel):
                name = operation.name_lower
            elif isinstance(
                operation,
                (
                    migrations.AddField,
                    migrations.AlterField,
                    migrations.AddConstraint,
                    migrations.RemoveConstraint,
                    migrations.AddIndex,
                ),
            ):
                name = operation.model_name_lower
            else:
                msg = "Unsupported Wave C state operation"
                raise TypeError(msg)
            changed.add((app_label, name))
        state.apps = registry
        with registry.bulk_update():
            for key in changed:
                registry.unregister_model(*key)
        registry.render_multiple([state.models[key] for key in sorted(changed)])
        state.is_delayed = True

"""Historical-state operations that avoid unrelated tenant-model renders.

Database execution, routing, reversibility and optimizer flags remain Django's.
Only registry maintenance differs; no SQL or data operation is reordered.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from django.db import migrations

if TYPE_CHECKING:
    from django.db.migrations.state import ProjectState


class AddConstraint(migrations.AddConstraint):
    """Reload constraint metadata without rebuilding unchanged relationships.

    Constraint changes cannot change any field or reverse relation. Rebuild the
    affected model, leaving relationship snapshots delayed just as Django does
    for metadata changes. RunPython's clear_delayed_apps_cache() still rebuilds
    the complete registry before any historical ORM callback consumes it.
    """

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Apply Django's state change and refresh only its model metadata."""
        if "apps" not in state.__dict__:
            super().state_forwards(app_label, state)
            return
        registry = state.apps
        del state.apps
        super().state_forwards(app_label, state)
        state.apps = registry
        model_key = app_label, self.model_name_lower
        registry.unregister_model(*model_key)
        registry.render_multiple([state.models[model_key]])
        state.is_delayed = True


class SeparateDatabaseAndState(migrations.SeparateDatabaseAndState):
    """Render the final separated state, never its intermediate mutations.

    Django executes the database operations against their own intermediate
    states. The separate state_operations list therefore needs no historical
    registry until the whole batch is complete and a later consumer asks for it.
    """

    def state_forwards(self, app_label: str, state: ProjectState) -> None:
        """Invalidate once, then apply the unchanged state-operation sequence."""
        if self.state_operations:
            state.__dict__.pop("apps", None)
        super().state_forwards(app_label, state)

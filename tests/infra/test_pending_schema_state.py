"""The four scalar origin fields change no relationships or database operations."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.migrations._state_batch import SchemaStateBatch
from django.db import connection, migrations, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.writer import MigrationWriter

from infra.test_migration_rendering import _RecordingEditor

if TYPE_CHECKING:
    from django.db.migrations.operations.base import Operation
    from django.db.migrations.state import ProjectState


def _definitions(state: ProjectState) -> tuple[str, set[str]]:
    # Raw Field identity is not a migration-state oracle: some deconstructors
    # return newly cloned nested fields. Use the actual migration serializer.
    return MigrationWriter.serialize(
        [
            (k, m.name, sorted(m.fields.items()), m.options, m.bases, m.managers)
            for k, m in sorted(state.models.items())
        ]
    )


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(
    ("app", "name", "model", "fields"),
    [
        (
            "ai",
            "0023_pending_read_boundary",
            "AIArtifact",
            ("source_ref", "source_acceptance"),
        ),
        (
            "interop",
            "0005_export_pending_origins",
            "OrganizationExport",
            ("pending_origins",),
        ),
        ("interop", "0006_export_finalize", "OrganizationExport", ("build_origins",)),
    ],
)
def test_origin_batch_preserves_full_state_and_sql_in_both_directions(
    app: str, name: str, model: str, fields: tuple[str, ...]
) -> None:
    loader = MigrationLoader(None)
    operation = loader.disk_migrations[app, name].operations[0]
    assert isinstance(operation, SchemaStateBatch)
    field_type = models.UUIDField if app == "ai" else models.JSONField
    plain: list[Operation] = [
        migrations.AddField(
            model_name=model.lower(),
            name=key,
            field=field_type(null=True, editable=False),
        )
        for key in fields
    ]
    reference = migrations.SeparateDatabaseAndState(
        database_operations=plain, state_operations=plain
    )
    before = loader.project_state((app, name), at_end=False)
    _ = before.apps
    definitions = []
    statements = []
    for variant in (reference, operation):
        after = before.clone()
        variant.state_forwards(app, after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as forward:
            variant.database_forwards(app, forward, before.clone(), after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as backward:
            variant.database_backwards(app, backward, after, before.clone())
        assert forward.collected_sql
        assert backward.collected_sql
        statements.append((forward.collected_sql, backward.collected_sql))
        after.clear_delayed_apps_cache()
        historical = after.apps.get_model(app, model)
        for key in fields:
            field = historical._meta.get_field(key)
            assert isinstance(field, field_type)
            assert field.null
            assert not field.editable
            assert key not in before.models[app, model.lower()].fields
        definitions.append(_definitions(after))
    assert definitions[0] == definitions[1]
    assert statements[0] == statements[1]

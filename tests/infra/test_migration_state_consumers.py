"""Constraint metadata remains current for later schema and reverse consumers."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.migrations._state_batch import SchemaStateBatch
from django.db import connection, migrations, models
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.optimizer import MigrationOptimizer
from django.db.migrations.state import ModelState, ProjectState

from infra.test_migration_rendering import _RecordingEditor, rendered_state

if TYPE_CHECKING:
    from collections.abc import Sequence
    from typing import ClassVar

    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.operations.base import Operation

__all__ = ("rendered_state",)

pytestmark = pytest.mark.django_db(transaction=True)


def test_batch_constraint_metadata_reaches_later_alter_field() -> None:
    class CatalogEditor(_RecordingEditor):
        def _constraint_names(self, *_args: object, **kwargs: object) -> list[str]:
            assert kwargs["check"] is True
            excluded = kwargs["exclude"]
            assert isinstance(excluded, set)
            return [
                name
                for name in ("probe_implicit_nonnegative", "probe_at_least_five")
                if name not in excluded
            ]

    state = ProjectState()
    state.add_model(
        ModelState(
            "render_probe",
            "Entry",
            [
                ("id", models.AutoField(primary_key=True)),
                ("value", models.PositiveIntegerField()),
            ],
        )
    )
    _ = state.apps
    constraint = models.CheckConstraint(
        condition=models.Q(value__gte=5), name="probe_at_least_five"
    )
    statements = []
    for kind in (migrations.SeparateDatabaseAndState, SchemaStateBatch):
        before = state.clone()
        kind(
            state_operations=[
                migrations.AddConstraint(model_name="entry", constraint=constraint)
            ]
        ).state_forwards("render_probe", before)
        operation = migrations.AlterField(
            model_name="entry", name="value", field=models.IntegerField()
        )
        after = before.clone()
        operation.state_forwards("render_probe", after)
        with CatalogEditor(connection, collect_sql=True, atomic=False) as editor:
            operation.database_forwards("render_probe", editor, before, after)
        statements.append(list(editor.collected_sql))
        assert after.models["render_probe", "entry"].options["constraints"] == [
            constraint
        ]

    assert len(statements[0]) == 1
    assert "probe_implicit_nonnegative" in statements[0][0]
    assert "probe_at_least_five" not in statements[0][0]
    assert statements[1] == statements[0]


def test_batch_constraint_metadata_reaches_reverse_python_callbacks(
    rendered_state: ProjectState,
) -> None:
    constraint = models.CheckConstraint(
        condition=models.Q(value__gte=0), name="render_probe_nonnegative"
    )
    seen: list[list[str]] = []

    def observe(apps: Apps, _editor: BaseDatabaseSchemaEditor) -> None:
        constraints = apps.get_model("render_probe", "Entry")._meta.constraints
        seen.append([item.name for item in constraints])

    observations = []
    for kind in (migrations.SeparateDatabaseAndState, SchemaStateBatch):
        seen.clear()
        operation = migrations.AddConstraint(model_name="entry", constraint=constraint)
        batch = kind(database_operations=[operation], state_operations=[operation])
        callback = migrations.RunPython(observe, reverse_code=observe)
        before = rendered_state.clone()
        after = before.clone()
        batch.state_forwards("render_probe", after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as editor:
            callback.database_backwards("render_probe", editor, after, before)
            callback.database_forwards("render_probe", editor, after, before)

            class ConsumerMigration(migrations.Migration):
                operations: ClassVar[Sequence[Operation]] = (
                    MigrationOptimizer().optimize([batch, callback], "render_probe")
                )

            migration = ConsumerMigration("consumer_probe", "render_probe")
            migration.unapply(rendered_state.clone(), editor)
        observations.append(list(seen))

    assert observations[0] == [[constraint.name]] * 3
    assert observations[1] == observations[0]


def test_evaluation_batch_metadata_reaches_schema_and_python_consumers() -> None:
    loader = MigrationLoader(None)
    migration = loader.disk_migrations["ai", "0005_evaluation"]
    batch = migration.operations[0]
    assert isinstance(batch, migrations.SeparateDatabaseAndState)
    assert batch.atomic is True
    assert batch.reversible is False
    before = loader.project_state(migration.dependencies)
    observations = []
    statements = []
    constraint = models.CheckConstraint(
        condition=models.Q(automatic_failures__gte=0),
        name="evaluation_consumer_nonnegative",
    )
    seen: list[tuple[int, list[str], list[str]]] = []

    def observe(apps: Apps, _editor: BaseDatabaseSchemaEditor) -> None:
        configuration = apps.get_model("ai", "ModelConfiguration")
        rating = apps.get_model("ai", "EvaluationRating")
        run = apps.get_model("ai", "EvaluationRun")
        field = configuration._meta.get_field("prompt_sha256")
        assert isinstance(field, models.CharField)
        assert field.max_length is not None
        seen.append(
            (
                field.max_length,
                [item.name for item in rating._meta.constraints],
                [item.name for item in run._meta.constraints],
            )
        )

    for operation in (
        migrations.SeparateDatabaseAndState(
            database_operations=batch.database_operations,
            state_operations=batch.state_operations,
        ),
        batch,
    ):
        seen.clear()
        state = before.clone()
        _ = state.apps
        operation.state_forwards("ai", state)
        callback = migrations.RunPython(observe, reverse_code=observe)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as editor:
            # Reverse callbacks must be correct before a forward cache clear.
            callback.database_backwards("ai", editor, state, before)
            callback.database_forwards("ai", editor, state, before)
            following_constraint = migrations.AddConstraint(
                model_name="evaluationrun", constraint=constraint
            )
            for following in (
                migrations.AlterField(
                    model_name="modelconfiguration",
                    name="prompt_sha256",
                    field=models.CharField(default="", max_length=63),
                ),
                type(operation)(
                    database_operations=[following_constraint],
                    state_operations=[following_constraint],
                ),
            ):
                after = state.clone()
                following.state_forwards("ai", after)
                following.database_forwards("ai", editor, state, after)
                state = after
            callback.database_backwards("ai", editor, state, before)
            callback.database_forwards("ai", editor, state, before)
        observations.append(list(seen))
        statements.append(list(editor.collected_sql))

    rating_constraints = ["ai_eval_rating_slot", "ai_eval_rating_human"]
    assert observations[0] == [
        (64, rating_constraints, []),
        (64, rating_constraints, []),
        (63, rating_constraints, [constraint.name]),
        (63, rating_constraints, [constraint.name]),
    ]
    assert observations[1] == observations[0]
    assert statements[1] == statements[0]

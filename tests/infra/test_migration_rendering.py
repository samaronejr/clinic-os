from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.ai.migrations._state_batch import SchemaStateBatch
from django.db import connection, migrations, models
from django.db.backends.postgresql.schema import DatabaseSchemaEditor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ModelState, ProjectState

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from django.apps.registry import Apps
    from django.db.backends.ddl_references import Statement
    from django.db.migrations.migration import Migration


class _RecordingEditor(DatabaseSchemaEditor):
    """PostgreSQL schema editor that records each statement instead of running it."""

    def execute(
        self, sql: Statement | str, params: Sequence[object] | None = ()
    ) -> None:
        self.collected_sql.append(f"{sql} {params!r}")


@pytest.fixture
def rendered_state() -> ProjectState:
    state = ProjectState()
    state.add_model(
        ModelState(
            "render_probe", "Organization", [("id", models.AutoField(primary_key=True))]
        )
    )
    for name in ("Entry", "Neighbor"):
        state.add_model(
            ModelState(
                "render_probe",
                name,
                [
                    ("id", models.AutoField(primary_key=True)),
                    (
                        "organization",
                        models.ForeignKey("render_probe.Organization", models.CASCADE),
                    ),
                    ("value", models.IntegerField(default=0)),
                ],
            )
        )
    _ = state.apps
    return state


def _migration_plan(loader: MigrationLoader) -> Iterator[Migration]:
    seen: set[tuple[str, str]] = set()
    for target in loader.graph.leaf_nodes():
        for key in loader.graph.forwards_plan(target):
            if key not in seen:
                seen.add(key)
                yield loader.graph.nodes[key]


def test_constraint_state_renders_only_the_changed_model(
    rendered_state: ProjectState, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a real historical constraint operation and a connected model graph.
    loader = MigrationLoader(None)
    historical = next(
        operation
        for operation in loader.disk_migrations[
            "ehr", "0005_clinical_attachment"
        ].operations
        if isinstance(operation, migrations.AddConstraint)
    )
    constraint = models.CheckConstraint(
        condition=models.Q(value__gte=0), name="render_probe_nonnegative"
    )
    operation = type(historical)(model_name="entry", constraint=constraint)
    before = rendered_state.clone()
    rendered: list[str] = []
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        rendered.append(model.name_lower)
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)

    # When: only constraint metadata changes.
    operation.state_forwards("render_probe", rendered_state)

    # Then: unrelated models are not rebuilt, and the prior snapshot stays exact.
    assert rendered == ["entry"]
    assert before.apps.get_model("render_probe", "Entry")._meta.constraints == []
    assert rendered_state.apps.get_model("render_probe", "Entry")._meta.constraints == [
        constraint
    ]
    rendered_state.clear_delayed_apps_cache()
    assert rendered_state.apps.get_model("render_probe", "Entry")._meta.get_field(
        "organization"
    ).related_model is rendered_state.apps.get_model("render_probe", "Organization")


def test_separated_state_changes_render_only_when_consumed(
    rendered_state: ProjectState, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: a historical separated operation with multiple state-only changes.
    loader = MigrationLoader(None)
    historical = next(
        operation
        for operation in loader.disk_migrations[
            "intake", "0011_protected_fields"
        ].operations
        if isinstance(operation, migrations.SeparateDatabaseAndState)
    )
    operation = type(historical)(
        state_operations=[
            migrations.AddField(
                model_name="entry", name="first", field=models.IntegerField(null=True)
            ),
            migrations.AddField(
                model_name="entry", name="second", field=models.IntegerField(null=True)
            ),
        ]
    )
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render
    rendered: list[str] = []

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        rendered.append(model.name_lower)
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)

    # When: the complete state-only batch is applied.
    operation.state_forwards("render_probe", rendered_state)

    # Then: no intermediate registry is built; consumers see both final fields.
    assert rendered == []
    model = rendered_state.apps.get_model("render_probe", "Entry")
    assert {field.name for field in model._meta.local_fields} == {
        "id",
        "organization",
        "value",
        "first",
        "second",
    }
    assert sorted(rendered) == ["entry", "neighbor", "organization"]


def test_full_migration_state_replay_has_bounded_model_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the entire real clean-install migration graph and its real render path.
    loader = MigrationLoader(None)
    state = ProjectState(real_apps=loader.unmigrated_apps)
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render
    rendered = 0

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        nonlocal rendered
        rendered += 1
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)

    # When: the executor's complete forward state transitions are replayed.
    for migration in _migration_plan(loader):
        _ = state.apps
        for operation in migration.operations:
            operation.state_forwards(migration.app_label, state)

    # Then: repeated tenant-graph renders cannot return unnoticed.
    assert rendered < 25_000


def test_wave_c_batch_renders_changed_models_once(
    rendered_state: ProjectState, monkeypatch: pytest.MonkeyPatch
) -> None:
    operation = SchemaStateBatch(
        state_operations=[
            migrations.AddField(
                model_name="entry", name="first", field=models.IntegerField(null=True)
            ),
            migrations.AddField(
                model_name="entry", name="second", field=models.IntegerField(null=True)
            ),
            migrations.CreateModel(
                name="Receipt",
                fields=[
                    ("id", models.AutoField(primary_key=True)),
                    ("entry", models.ForeignKey("render_probe.Entry", models.CASCADE)),
                ],
            ),
        ]
    )
    before = rendered_state.clone()
    rendered: list[str] = []
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        rendered.append(model.name_lower)
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)
    operation.state_forwards("render_probe", rendered_state)

    assert rendered == ["entry", "receipt"]
    assert "first" not in before.models["render_probe", "entry"].fields
    assert {"first", "second"} <= set(
        rendered_state.models["render_probe", "entry"].fields
    )
    rendered_state.clear_delayed_apps_cache()
    receipt = rendered_state.apps.get_model("render_probe", "Receipt")
    entry = rendered_state.apps.get_model("render_probe", "Entry")
    assert receipt._meta.get_field("entry").related_model is entry
    assert {relation.related_model for relation in entry._meta.related_objects} == {
        receipt
    }


def test_wave_c_batch_database_path_matches_separated_sql(
    rendered_state: ProjectState, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Given: one batch through Django's separated operation and through the helper.
    operations = [
        migrations.AddField(
            model_name="entry", name="first", field=models.IntegerField(null=True)
        ),
        migrations.AddConstraint(
            model_name="entry",
            constraint=models.CheckConstraint(
                condition=models.Q(first__gte=0), name="render_probe_first"
            ),
        ),
        migrations.CreateModel(
            name="Receipt",
            fields=[
                ("id", models.AutoField(primary_key=True)),
                ("entry", models.ForeignKey("render_probe.Entry", models.CASCADE)),
            ],
        ),
        migrations.AddIndex(
            model_name="receipt",
            index=models.Index(fields=["entry", "id"], name="render_probe_receipt"),
        ),
    ]
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render
    rendered: list[str] = []

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        rendered.append(model.name_lower)
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)
    statements: dict[type[migrations.SeparateDatabaseAndState], list[str]] = {}
    renders: dict[type[migrations.SeparateDatabaseAndState], list[str]] = {}
    for kind in (migrations.SeparateDatabaseAndState, SchemaStateBatch):
        operation = kind(database_operations=operations, state_operations=operations)
        before = rendered_state.clone()
        after = before.clone()
        operation.state_forwards("render_probe", after)
        rendered.clear()

        # When: the database path runs forwards and then backwards.
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as forward:
            operation.database_forwards("render_probe", forward, before, after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as backward:
            operation.database_backwards("render_probe", backward, after, before)
        statements[kind] = [*forward.collected_sql, *backward.collected_sql]
        renders[kind] = list(rendered)

    # Then: Django's exact SQL, with one render per database operation.
    separated = statements[migrations.SeparateDatabaseAndState]
    assert statements[SchemaStateBatch] == separated
    assert renders[SchemaStateBatch] == ["entry", "entry", "receipt", "receipt"] * 2

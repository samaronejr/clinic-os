from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, cast

import pytest
from apps.ai.migrations._state_batch import SchemaStateBatch
from django.db import DEFAULT_DB_ALIAS, connection, migrations, models
from django.db.backends.postgresql.schema import DatabaseSchemaEditor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.state import ModelState, ProjectState
from django.db.migrations.writer import MigrationWriter

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator, Sequence

    from django.apps.registry import Apps
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.backends.ddl_references import Statement
    from django.db.migrations.migration import Migration
    from django.db.migrations.operations.base import Operation


class _RecordingEditor(DatabaseSchemaEditor):
    """PostgreSQL schema editor that records each statement instead of running it."""

    def execute(
        self, sql: Statement | str, params: Sequence[object] | None = ()
    ) -> None:
        self.collected_sql.append(f"{sql} {params!r}")


class _ExecutesNothing:
    """Clean-install editor double: every schema and SQL call executes nothing.

    Its connection is not PostgreSQL, so extension probes skip catalog queries.
    """

    atomic_migration = True
    connection = SimpleNamespace(
        alias=DEFAULT_DB_ALIAS,
        vendor="render-replay",
        ops=SimpleNamespace(prepare_sql_script=lambda sql: [sql]),
    )

    def __getattr__(self, name: str) -> Callable[..., None]:
        return _ignore


def _ignore(*args: object, **kwargs: object) -> None:
    """Accept a schema or historical data call without touching a database."""


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


def _with_database_halves(operations: Sequence[Operation]) -> Iterator[Operation]:
    """Each operation, then the database half of any SeparateDatabaseAndState."""
    for operation in operations:
        yield operation
        if isinstance(operation, migrations.SeparateDatabaseAndState):
            yield from _with_database_halves(operation.database_operations)


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


def test_full_migrate_path_has_bounded_model_renders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Given: the clean-install graph with its database operations and callbacks.
    loader = MigrationLoader(None)
    plan = list(_migration_plan(loader))
    for migration in plan:
        for operation in _with_database_halves(migration.operations):
            if isinstance(operation, migrations.RunPython):
                monkeypatch.setattr(operation, "code", _ignore)
    state = ProjectState(real_apps=loader.unmigrated_apps)
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render
    rendered = 0

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        nonlocal rendered
        rendered += 1
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)

    # When: migrate applies every migration through Django's state and database
    # paths, then refreshes the delayed registry for post_migrate receivers.
    editor = cast("BaseDatabaseSchemaEditor", _ExecutesNothing())
    for migration in plan:
        if "apps" not in state.__dict__:
            _ = state.apps
        state = migration.apply(state, editor)
    state.clear_delayed_apps_cache()
    _ = state.apps

    # Then: separated database operations cannot hide repeated renders, and the
    # clean release migrate keeps its measured margin under the 30 s probe.
    assert rendered < 35_000


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


@pytest.mark.parametrize("minimum", [0, 1])
def test_wave_c_batch_preserves_unchanged_model_state_and_sql(
    rendered_state: ProjectState, monkeypatch: pytest.MonkeyPatch, minimum: int
) -> None:
    constraint = models.CheckConstraint(
        condition=models.Q(value__gte=0), name="render_probe_nonnegative"
    )
    migrations.AddConstraint(model_name="entry", constraint=constraint).state_forwards(
        "render_probe", rendered_state
    )
    replacement = models.CheckConstraint(
        condition=models.Q(value__gte=minimum), name="render_probe_nonnegative"
    )
    operations = [
        migrations.RemoveConstraint(
            model_name="entry", name="render_probe_nonnegative"
        ),
        migrations.AddConstraint(model_name="entry", constraint=replacement),
    ]
    original: Callable[[ModelState, Apps], type[models.Model]] = ModelState.render
    renders: list[str] = []

    def render(model: ModelState, apps: Apps) -> type[models.Model]:
        renders.append(model.name_lower)
        return original(model, apps)

    monkeypatch.setattr(ModelState, "render", render)
    statements = []
    definitions = []
    batch_renders: list[str] = []
    for kind in (migrations.SeparateDatabaseAndState, SchemaStateBatch):
        operation = kind(database_operations=operations, state_operations=operations)
        before = rendered_state.clone()
        after = before.clone()
        renders.clear()
        operation.state_forwards("render_probe", after)
        if kind is SchemaStateBatch:
            batch_renders = list(renders)
        assert after.apps.get_model("render_probe", "Entry")._meta.constraints == [
            replacement
        ]
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as forward:
            operation.database_forwards("render_probe", forward, before.clone(), after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as backward:
            operation.database_backwards(
                "render_probe", backward, after, before.clone()
            )
        statements.append([*forward.collected_sql, *backward.collected_sql])
        after.clear_delayed_apps_cache()
        definitions.append(
            MigrationWriter.serialize(
                [
                    (
                        key,
                        state.name,
                        sorted(state.fields.items()),
                        state.options,
                        state.bases,
                        state.managers,
                    )
                    for key, state in sorted(after.models.items())
                ]
            )
        )
        entry = after.apps.get_model("render_probe", "Entry")
        organization = after.apps.get_model("render_probe", "Organization")
        field = entry._meta.get_field("organization")
        assert field.related_model is organization
        assert any(
            getattr(relation, "field", None) is field
            for relation in organization._meta.get_fields(include_hidden=True)
        )
        assert entry._meta.constraints == [replacement]
        assert before.apps.get_model("render_probe", "Entry")._meta.constraints == [
            constraint
        ]

    assert statements[0] == statements[1]
    assert definitions[0] == definitions[1]
    assert batch_renders == ["entry"]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize("care_models_present", [False, True])
def test_authority_batch_preserves_fk_sql_and_historical_relations(
    care_models_present: bool,
) -> None:
    loader = MigrationLoader(None)
    batch = loader.disk_migrations["ai", "0008_front_desk_authority"].operations[0]
    assert isinstance(batch, SchemaStateBatch)
    separated = migrations.SeparateDatabaseAndState(
        database_operations=batch.database_operations,
        state_operations=batch.state_operations,
    )
    targets = [("ai", "0007_front_desk")]
    if care_models_present:
        targets.append(("ai", "0008_care_plan_status_private"))
    before = loader.project_state(targets)
    assert (("ai", "careproposal") in before.models) is care_models_present
    _ = before.apps
    statements = []
    states = []
    definitions = []
    for operation in (separated, batch):
        after = before.clone()
        operation.state_forwards("ai", after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as forward:
            operation.database_forwards("ai", forward, before.clone(), after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as backward:
            operation.database_backwards("ai", backward, after, before.clone())
        statements.append([*forward.collected_sql, *backward.collected_sql])
        # Historical ORM callbacks consume this same delayed-registry boundary.
        after.clear_delayed_apps_cache()
        _ = after.apps
        states.append(after)
        definitions.append(
            MigrationWriter.serialize(
                [
                    (
                        key,
                        state.name,
                        sorted(state.fields.items()),
                        state.options,
                        state.bases,
                        state.managers,
                    )
                    for key, state in sorted(after.models.items())
                ]
            )
        )

    assert statements[0] == statements[1]
    # ArrayField.deconstruct() clones its base field; compare full serialized
    # definitions, including nested fields, rather than their creation counters.
    assert definitions[0] == definitions[1]
    for state_operation in batch.state_operations:
        if not isinstance(state_operation, migrations.AddField):
            continue
        model = states[1].apps.get_model("ai", state_operation.model_name)
        field = model._meta.get_field(state_operation.name)
        if not isinstance(field, models.ForeignKey):
            continue
        assert field.null
        assert field.remote_field is not None
        assert field.remote_field.related_name == "+"
        target = field.remote_field.model
        assert not isinstance(target, str)
        assert target is states[1].apps.get_model(target._meta.label_lower)
        assert any(
            getattr(relation, "field", None) is field
            for relation in target._meta.get_fields(include_hidden=True)
        )


@pytest.mark.django_db(transaction=True)
def test_journal_batch_preserves_sql_and_historical_relations() -> None:
    # Given: billing 0011's batched journal models and Django's separated form.
    loader = MigrationLoader(None)
    migration = loader.disk_migrations["billing", "0011_operational_journal"]
    batch = migration.operations[0]
    assert isinstance(batch, SchemaStateBatch)
    separated = migrations.SeparateDatabaseAndState(
        database_operations=batch.database_operations,
        state_operations=batch.state_operations,
    )
    before = loader.project_state(migration.dependencies)
    _ = before.apps
    statements = []
    states = []
    definitions = []
    for operation in (separated, batch):
        # When: each applies its state and runs both database directions.
        after = before.clone()
        operation.state_forwards("billing", after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as forward:
            operation.database_forwards("billing", forward, before.clone(), after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as backward:
            operation.database_backwards("billing", backward, after, before.clone())
        statements.append([*forward.collected_sql, *backward.collected_sql])
        after.clear_delayed_apps_cache()
        _ = after.apps
        states.append(after)
        definitions.append(
            MigrationWriter.serialize(
                [
                    (
                        key,
                        state.name,
                        sorted(state.fields.items()),
                        state.options,
                        state.bases,
                        state.managers,
                    )
                    for key, state in sorted(after.models.items())
                ]
            )
        )

    # Then: the same DDL both ways, the same definitions and live relations.
    assert statements[0] == statements[1]
    assert definitions[0] == definitions[1]
    for created in batch.state_operations:
        assert isinstance(created, migrations.CreateModel)
        model = states[1].apps.get_model("billing", created.name)
        for field in model._meta.concrete_fields:
            if field.remote_field is None:
                continue
            target = field.remote_field.model
            assert not isinstance(target, str)
            assert target is states[1].apps.get_model(target._meta.label_lower)
            assert any(
                getattr(relation, "field", None) is field
                for relation in target._meta.get_fields(include_hidden=True)
            )


# Wave E and the heaviest earlier migrations batch their state; each batch must
# emit exactly the separated form's SQL and leave exactly its model state.
BATCHED = [
    ("ai", "0010_revenue_cycle"),
    ("analytics", "0001_initial"),
    ("billing", "0012_sandbox_psp"),
    ("consent", "0004_purpose_taxonomy"),
    ("crm", "0001_initial"),
    ("ehr", "0014_longitudinal_record"),
    ("insurance", "0001_revenue_cycle"),
    ("inventory", "0001_inventory"),
    ("orders", "0001_identity_safe_results"),
    ("prescription", "0014_independent_signature_verification"),
    ("teleconsult", "0004_teleconsult_v2"),
]


@pytest.mark.django_db(transaction=True)
@pytest.mark.parametrize(("app", "name"), BATCHED)
def test_wave_e_batches_preserve_sql_and_state(app: str, name: str) -> None:
    # Given: the batched migration and Django's separated form of its batch.
    loader = MigrationLoader(None)
    migration = loader.disk_migrations[app, name]
    index = next(
        position
        for position, operation in enumerate(migration.operations)
        if isinstance(operation, SchemaStateBatch)
    )
    batch = migration.operations[index]
    assert isinstance(batch, SchemaStateBatch)
    separated = migrations.SeparateDatabaseAndState(
        database_operations=batch.database_operations,
        state_operations=batch.state_operations,
    )
    # The state just before this migration (dependencies may name __first__).
    before = loader.project_state((app, name), at_end=False)
    for operation in migration.operations[:index]:
        operation.state_forwards(app, before)
    _ = before.apps
    statements = []
    definitions = []
    # When: both forms run forward from the same state.
    for operation in (separated, batch):
        after = before.clone()
        operation.state_forwards(app, after)
        with _RecordingEditor(connection, collect_sql=True, atomic=False) as forward:
            operation.database_forwards(app, forward, before.clone(), after)
        statements.append(forward.collected_sql)
        after.clear_delayed_apps_cache()
        _ = after.apps
        definitions.append(
            MigrationWriter.serialize(
                [
                    (
                        key,
                        state.name,
                        sorted(state.fields.items()),
                        state.options,
                        state.bases,
                        state.managers,
                    )
                    for key, state in sorted(after.models.items())
                ]
            )
        )
    # Then: identical database statements and identical final model state.
    assert statements[0] == statements[1]
    assert definitions[0] == definitions[1]

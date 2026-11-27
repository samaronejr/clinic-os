"""Run the guarded command from real, independently observed commit boundaries."""

from __future__ import annotations

import io
import json
import os
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from hashlib import sha256
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.tenancy import migration_preflight
from apps.tenancy.migrations import _resumable_schema as recovery
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connections, transaction
from django.db.backends.postgresql.base import DatabaseWrapper
from django.db.migrations.exceptions import IrreversibleError
from django.db.migrations.executor import MigrationExecutor
from django.db.migrations.loader import MigrationLoader
from django.db.migrations.recorder import MigrationRecorder
from django.utils import timezone
from psycopg import sql

from database_urls import database_url_for_name
from patients.test_intake_migrations import (
    _default_connection,
    _scratch_database,
    _scratch_tenant_key,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from django.db.backends.base.base import BaseDatabaseWrapper
    from django.db.migrations import Migration
    from django.db.migrations.state import ProjectState
    from pytest_django.plugin import DjangoDbBlocker

INTAKE = ("intake", "0013_patient_demographics")
AI = ("ai", "0003_artifact_retention")


class InterruptionError(Exception):
    """Simulate a lost client after PostgreSQL commits but before Django records."""


def _guarded() -> dict[recovery.MigrationKey, int]:
    return {
        key: len(migration.operations)
        for key, migration in MigrationLoader(None).disk_migrations.items()
        if isinstance(migration, recovery.GuardedMigration)
    }


def _atomic_schemas() -> set[recovery.MigrationKey]:
    return {
        key
        for key, migration in MigrationLoader(None).disk_migrations.items()
        if isinstance(migration, recovery.AtomicSchemaMigration)
    }


def _applied(db: DatabaseWrapper) -> set[recovery.MigrationKey]:
    return set(MigrationExecutor(db).recorder.applied_migrations())


@contextmanager
def _clone(admin_url: str, source: DatabaseWrapper) -> Iterator[DatabaseWrapper]:
    name = f"mig_resume_{uuid4().hex[:12]}"
    source.close()
    with psycopg.connect(admin_url, autocommit=True) as admin:
        admin.execute(
            sql.SQL("CREATE DATABASE {} OWNER clinic_owner TEMPLATE {}").format(
                sql.Identifier(name), sql.Identifier(source.settings_dict["NAME"])
            )
        )
        db = DatabaseWrapper({**source.settings_dict, "NAME": name}, alias=name)
        connections[db.alias] = db
        try:
            yield db
        finally:
            db.close()
            del connections[db.alias]
            admin.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name))
            )


@dataclass(frozen=True)
class Plan:
    admin_url: str
    source: dict[recovery.MigrationKey, DatabaseWrapper]
    head_schema: str
    head_ledger: set[recovery.MigrationKey]


@pytest.fixture(scope="module")
def plan(django_db_blocker: DjangoDbBlocker) -> Iterator[Plan]:
    """Build each predecessor by walking an ordinary all-leaves plan once."""
    admin_url = os.environ["TEST_SUPERUSER_DATABASE_URL"]
    with (
        django_db_blocker.unblock(),
        ExitStack() as templates,
        _scratch_database(admin_url) as db,
        _default_connection(db),
    ):
        source: dict[recovery.MigrationKey, DatabaseWrapper] = {}

        def before(action: str, *args: object) -> None:
            if action == "apply_start" and isinstance(
                args[0], (recovery.GuardedMigration, recovery.AtomicSchemaMigration)
            ):
                migration = args[0]
                source[migration.app_label, migration.name] = templates.enter_context(
                    _clone(admin_url, db)
                )

        executor = MigrationExecutor(db, progress_callback=before)
        executor.migrate(executor.loader.graph.leaf_nodes())
        assert source.keys() == _guarded().keys() | _atomic_schemas()
        yield Plan(
            admin_url, source, migration_preflight.schema_digest(db), _applied(db)
        )


def _run(db: DatabaseWrapper) -> None:
    with _default_connection(db):
        call_command("migrate", interactive=False, verbosity=0, stdout=io.StringIO())


def _cut(
    db: DatabaseWrapper,
    admin_url: str,
    key: recovery.MigrationKey,
    step: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    committed = Event()
    record = recovery.record_step

    def record_and_interrupt(
        connection: BaseDatabaseWrapper, migration: recovery.MigrationKey, ordinal: int
    ) -> None:
        record(connection, migration, ordinal)
        if (migration, ordinal) != (key, step):
            return

        def stop() -> None:
            # Re-read from another session: a Python call before COMMIT does not
            # count, nor does a cut after Django's migration recorder insertion.
            with psycopg.connect(
                database_url_for_name(admin_url, db.settings_dict["NAME"])
            ) as observer:
                rows = observer.execute(
                    "SELECT step FROM clinic_migration.progress "
                    "WHERE app=%s AND name=%s ORDER BY step",
                    key,
                ).fetchall()
                assert rows == [(n,) for n in range(1, step + 1)]
                assert (
                    observer.execute(
                        "SELECT 1 FROM clinic_app.django_migrations "
                        "WHERE app=%s AND name=%s",
                        key,
                    ).fetchone()
                    is None
                )
            committed.set()
            raise InterruptionError

        connection.on_commit(stop)

    with monkeypatch.context() as patch:
        patch.setattr(recovery, "record_step", record_and_interrupt)
        with pytest.raises(InterruptionError):
            _run(db)
    assert committed.is_set()
    db.close()


def _seed(
    db: DatabaseWrapper, key: recovery.MigrationKey
) -> Callable[[], tuple[object, ...]]:
    """Use actual historical ORM fields, key issuance and RLS to seed a row."""
    loader = MigrationLoader(db)
    registry = loader.project_state(list(loader.applied_migrations)).apps
    org = uuid4()
    with (
        _default_connection(db),
        transaction.atomic(using=db.alias),
        db.cursor() as cursor,
    ):
        cursor.execute("SELECT set_config('app.current_tenant', %s, true)", [str(org)])
        registry.get_model("identity", "Organization").objects.using(db.alias).create(
            id=org, name="Synthetic Migration Race", cnpj=f"{org.int % 10**14:014d}"
        )
        _scratch_tenant_key(db, org)
        if key == AI:
            clinic = (
                registry.get_model("identity", "Clinic")
                .objects.using(db.alias)
                .create(
                    organization_id=org,
                    name="Synthetic Clinic",
                    crm_uf="SP",
                    timezone="America/Sao_Paulo",
                )
            )
            user = (
                registry.get_model("identity", "User")
                .objects.using(db.alias)
                .create(username=f"synthetic-resume-{org.hex}")
            )
            registry.get_model("identity", "UserClinicRole").objects.using(
                db.alias
            ).create(
                user_id=user.pk, clinic_id=clinic.pk, organization_id=org, role="owner"
            )
            cursor.execute(
                "SELECT set_config('app.current_user_id', %s, true)", [str(user.pk)]
            )
            item = (
                registry.get_model("ai", "AIArtifact")
                .objects.using(db.alias)
                .create(
                    organization_id=org,
                    clinic_id=clinic.pk,
                    user_id=user.pk,
                    purpose="front_desk",
                    retention_class="transient_input",
                    content="Synthetic migration artifact",
                    expires_at=timezone.now() + timedelta(days=1),
                )
            )
            read_sql = (
                "SELECT content, retention_class "
                "FROM clinic_app.ai_aiartifact WHERE id=%s"
            )
        else:
            item = (
                registry.get_model("intake", "Patient")
                .objects.using(db.alias)
                .create(
                    organization_id=org,
                    full_name="Synthetic Migration Patient",
                    birth_date=date(2001, 1, 1),
                )
            )
            read_sql = (
                "SELECT full_name, birth_date FROM clinic_app.intake_patient "
                "WHERE id=%s"
            )

    def read() -> tuple[object, ...]:
        with transaction.atomic(using=db.alias), db.cursor() as cursor:
            cursor.execute(
                "SELECT set_config('app.current_tenant', %s, true)", [str(org)]
            )
            cursor.execute(read_sql, [item.pk])
            row: tuple[object, ...] | None = cursor.fetchone()
        assert row is not None
        assert row[0]
        return row

    return read


@pytest.mark.parametrize(
    ("key", "step"),
    [
        pytest.param(key, step, id=f"{'.'.join(key)}-commit-{step}")
        for key, length in _guarded().items()
        for step in range(1, length + 1)
    ],
)
def test_resume_after_each_committed_boundary(
    key: recovery.MigrationKey, step: int, plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _clone(plan.admin_url, plan.source[key]) as db:
        read = _seed(db, key) if key in (INTAKE, AI) else tuple
        original = read()
        _cut(db, plan.admin_url, key, step, monkeypatch)
        assert key not in _applied(db)
        _run(db)
        assert _applied(db) == plan.head_ledger
        assert migration_preflight.schema_digest(db) == plan.head_schema
        assert read() == original


def _assert_refused(db: DatabaseWrapper, admin_url: str, message: str) -> str:
    rows = _row_image(db, admin_url)
    schema = migration_preflight.schema_digest(db)
    with pytest.raises(CommandError, match=message) as refusal:
        _run(db)
    assert _row_image(db, admin_url) == rows
    assert migration_preflight.schema_digest(db) == schema
    return str(refusal.value)


def _row_image(db: DatabaseWrapper, admin_url: str) -> str:
    """Observe every user row, sequence, receipt and exact Django ledger row."""
    digest = sha256()
    dsn = database_url_for_name(admin_url, db.settings_dict["NAME"])
    with psycopg.connect(dsn) as observer:
        relations = observer.execute(
            """
            SELECT n.nspname, c.relname, c.relkind
            FROM pg_catalog.pg_class c
            JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace
            WHERE n.nspname NOT IN ('pg_catalog', 'information_schema')
                AND n.nspname NOT LIKE 'pg_toast%%'
                AND c.relkind IN ('r', 'S', 'm', 'p')
            ORDER BY 1, 2
            """
        ).fetchall()
        for namespace, table, kind in relations:
            relation = sql.Identifier(namespace, table)
            # Sequences have state but no composite row type. Keep the exact
            # allocator state in the oracle instead of dropping sequences.
            query = (
                sql.SQL("SELECT last_value, log_cnt, is_called FROM {}").format(
                    relation
                )
                if kind == "S"
                else sql.SQL("SELECT to_jsonb(r)::text FROM {} r ORDER BY 1").format(
                    relation
                )
            )
            rows = observer.execute(query).fetchall()
            digest.update(json.dumps([namespace, table, rows]).encode())
    return digest.hexdigest()


@pytest.mark.parametrize(
    ("sql_mutation", "message"),
    [
        pytest.param(
            "DELETE FROM clinic_migration.progress WHERE app='intake' AND "
            "name='0013_patient_demographics'",
            "entry logical schema differs.*restore or rebuild",
            id="drop-progress",
        ),
        pytest.param(
            "UPDATE clinic_migration.progress SET step=100 WHERE app='intake' "
            "AND name='0013_patient_demographics' AND step=2",
            "invalid progress prefix.*restore or rebuild",
            id="corrupt-progress",
        ),
        pytest.param(
            "INSERT INTO clinic_migration.progress VALUES "
            "('intake', '0013_patient_demographics', 3)",
            "entry logical schema differs.*restore or rebuild",
            id="skip-step",
        ),
        pytest.param(
            "CREATE TABLE public.unexpected_f6_object (id integer)",
            "entry logical schema differs.*restore or rebuild",
            id="unexpected-object",
        ),
    ],
)
def test_refuse_unexplained_cut_before_writing(
    sql_mutation: str, message: str, plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _clone(plan.admin_url, plan.source[INTAKE]) as db:
        _seed(db, INTAKE)
        _cut(db, plan.admin_url, INTAKE, 2, monkeypatch)
        with db.cursor() as cursor:
            cursor.execute(sql_mutation)
        _assert_refused(db, plan.admin_url, message)


def test_unaccent_dictionary_drift_refuses_then_same_database_retries(
    plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _clone(plan.admin_url, plan.source[INTAKE]) as db:
        read = _seed(db, INTAKE)
        original = read()
        _cut(db, plan.admin_url, INTAKE, 2, monkeypatch)
        # A trusted extension's member objects aren't owned by the role that
        # installed it. Model operator DDL with the isolated test administrator.
        dsn = database_url_for_name(plan.admin_url, db.settings_dict["NAME"])
        with psycopg.connect(dsn, autocommit=True) as admin, admin.cursor() as cursor:
            cursor.execute(
                """
                SELECT n.nspname, dict.dictname
                FROM pg_catalog.pg_ts_dict dict
                JOIN pg_catalog.pg_namespace n ON n.oid = dict.dictnamespace
                JOIN pg_catalog.pg_depend d ON d.objid = dict.oid
                    AND d.classid = 'pg_catalog.pg_ts_dict'::regclass
                    AND d.refclassid = 'pg_catalog.pg_extension'::regclass
                    AND d.deptype = 'e'
                JOIN pg_catalog.pg_extension e ON e.oid = d.refobjid
                WHERE e.extname = 'unaccent'
                """
            )
            [(namespace, name)] = cursor.fetchall()
            cursor.execute("SELECT clinic_app.unaccent('Hôtel')")
            assert cursor.fetchone() == ("Hotel",)
            cursor.execute(
                sql.SQL("ALTER TEXT SEARCH DICTIONARY {} RENAME TO {}").format(
                    sql.Identifier(namespace, name),
                    sql.Identifier("unexpected_dictionary"),
                )
            )
            with pytest.raises(
                psycopg.errors.UndefinedObject, match="text search dictionary"
            ):
                cursor.execute("SELECT clinic_app.unaccent('Hôtel')")
        _assert_refused(
            db, plan.admin_url, "entry logical schema differs.*restore or rebuild"
        )
        with psycopg.connect(dsn, autocommit=True) as admin:
            admin.execute(
                sql.SQL("ALTER TEXT SEARCH DICTIONARY {} RENAME TO {}").format(
                    sql.Identifier(namespace, "unexpected_dictionary"),
                    sql.Identifier(name),
                )
            )
        _run(db)
        assert _applied(db) == plan.head_ledger
        assert migration_preflight.schema_digest(db) == plan.head_schema
        assert read() == original


def test_operator_index_is_refused_then_retry_succeeds(plan: Plan) -> None:
    with _clone(plan.admin_url, plan.source[INTAKE]) as db:
        _seed(db, INTAKE)
        canary = "synthetic-index-definition-canary"
        with db.cursor() as cursor:
            cursor.execute(
                sql.SQL(
                    "CREATE INDEX f6_operator_index "
                    "ON clinic_app.intake_patient (organization_id) "
                    "WHERE organization_id::text <> {}"
                ).format(sql.Literal(canary))
            )
        diagnostic = _assert_refused(
            db,
            plan.admin_url,
            r"entry logical schema differs.*index drift: clinic_app\.f6_operator_index"
            r"; restore or rebuild",
        )
        assert canary not in diagnostic
        with db.cursor() as cursor:
            cursor.execute("DROP INDEX clinic_app.f6_operator_index")
        _run(db)
        assert migration_preflight.schema_digest(db) == plan.head_schema


def test_approved_base_vacuum_full_does_not_refuse_forward_plan(plan: Plan) -> None:
    # The SQL-layout fixture already enumerates the exact first-party baseline.
    source = Path(__file__).with_name("migration_sql_at_4f8d2454.json")
    added = json.loads(source.read_text(encoding="utf-8"))["added"]
    with _scratch_database(plan.admin_url) as db, _default_connection(db):
        executor = MigrationExecutor(db)
        loader = executor.loader
        nodes = {key for key in loader.graph.nodes if ".".join(key) not in added}
        targets = sorted(
            key
            for key in nodes
            if not any(
                child.key in nodes for child in loader.graph.node_map[key].children
            )
        )
        executor.migrate(targets)
        before = migration_preflight.schema_digest(db)
        legacy_relation = "clinic_app.ehr_clinicaldocumentversion"
        with db.cursor() as cursor:
            cursor.execute(
                "SELECT pg_relation_filenode(%s::regclass)", [legacy_relation]
            )
            old_filenode = cursor.fetchone()
            cursor.execute("VACUUM FULL clinic_app.ehr_clinicaldocumentversion")
            cursor.execute(
                "SELECT pg_relation_filenode(%s::regclass)", [legacy_relation]
            )
            new_filenode = cursor.fetchone()
        assert old_filenode != new_filenode
        assert migration_preflight.schema_digest(db) == before
        _run(db)
        assert _applied(db) == plan.head_ledger
        assert migration_preflight.schema_digest(db) == plan.head_schema


@pytest.mark.parametrize("key", sorted(_atomic_schemas()), ids=".".join)
def test_atomic_schema_and_django_record_share_a_commit(
    key: recovery.MigrationKey, plan: Plan, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _clone(plan.admin_url, plan.source[key]) as db, _default_connection(db):
        before = migration_preflight.schema_digest(db)
        applied = _applied(db)
        initial_progress = recovery.progress(db)
        record = MigrationRecorder.record_applied
        inserted = Event()

        def interrupt(recorder: MigrationRecorder, app: str, name: str) -> None:
            record(recorder, app, name)
            if (app, name) == key:
                assert db.in_atomic_block
                assert key in recorder.applied_migrations()
                inserted.set()
                raise InterruptionError

        executor = MigrationExecutor(db)
        assert [str(m) for m, _ in executor.migration_plan([key])] == [".".join(key)]
        with monkeypatch.context() as patch:
            patch.setattr(MigrationRecorder, "record_applied", interrupt)
            with pytest.raises(InterruptionError):
                executor.migrate([key])
        assert inserted.is_set()
        db.close()
        assert _applied(db) == applied
        assert migration_preflight.schema_digest(db) == before
        assert recovery.progress(db) == initial_progress

        MigrationExecutor(db).migrate([key])
        assert _applied(db) == applied | {key}
        assert migration_preflight.schema_digest(db) != before
        assert recovery.progress(db) == initial_progress


@pytest.mark.parametrize("key", [INTAKE, AI])
def test_guarded_migration_reverse_is_refused(
    key: recovery.MigrationKey, plan: Plan
) -> None:
    migration: Migration = MigrationLoader(None).disk_migrations[key]
    assert isinstance(migration, recovery.GuardedMigration)
    db = plan.source[key]
    loader = MigrationLoader(db)
    before: ProjectState = loader.project_state(list(loader.applied_migrations))
    with (
        db.schema_editor(atomic=False) as editor,
        pytest.raises(IrreversibleError, match="rollback requires restore"),
    ):
        migration.unapply(before, editor)

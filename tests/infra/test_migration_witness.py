"""Independent row, storage, and privilege controls for protected DDL recovery."""

from __future__ import annotations

import os
from copy import copy
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from apps.tenancy.migrations._resume_catalog import (
    Checkpoint,
    ResumeCatalogError,
    match_boundary,
    snapshot,
)
from django.db import DatabaseError, migrations, transaction
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from infra.test_migration_relationship_scope import _predecessor
from infra.test_protected_migration_resume import SimulatedInterruptionError
from patients.test_intake_migrations import _default_connection, _scratch_database

if TYPE_CHECKING:
    from collections.abc import Callable
    from uuid import UUID

    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.migrations import Migration
    from pytest_django.plugin import DjangoDbBlocker


@pytest.mark.parametrize(
    ("kind", "first", "second"),
    [
        ("numeric", "12.3400000000000001", "12.3400000000000002"),
        ("jsonb", "12.3400000000000001", "12.3400000000000002"),
        ("numeric", "12.340", "12.3400"),
        ("integer[]", "{2,3}", "[0:1]={2,3}"),
    ],
    ids=["exact-numeric", "exact-jsonb", "decimal-scale", "array-bounds"],
)
def test_retained_value_is_lossless(
    kind: str, first: str, second: str, django_db_blocker: DjangoDbBlocker
) -> None:
    types = {
        "numeric": sql.SQL("numeric"),
        "jsonb": sql.SQL("jsonb"),
        "integer[]": sql.SQL("integer[]"),
    }
    obj = "relation witness_probe"
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
        db.cursor() as cur,
    ):
        cur.execute("CREATE TABLE witness_probe (id integer PRIMARY KEY)")
        cur.execute("INSERT INTO witness_probe VALUES (1)")
        catalogs = []
        for literal in (first, second):
            cur.execute(
                sql.SQL("ALTER TABLE witness_probe ADD v {} DEFAULT {}").format(
                    types[kind], sql.Literal(literal)
                )
            )
            cur.execute("ALTER TABLE witness_probe ALTER v DROP DEFAULT")
            cur.execute("SELECT v::text FROM witness_probe WHERE id=1")
            assert cur.fetchone() == (literal,)
            cur.execute(
                "SELECT atthasmissing AND NOT atthasdef FROM pg_attribute "
                "WHERE attrelid='witness_probe'::regclass AND attname='v'"
            )
            assert cur.fetchone() == (True,)
            catalogs.append({obj: snapshot(db)[obj]})
            cur.execute("ALTER TABLE witness_probe DROP v")
        with pytest.raises(
            ResumeCatalogError, match=r"witness_probe\.v\.attmissingval"
        ):
            match_boundary(
                Checkpoint("value", frozenset({obj}), 1),
                catalogs[1],
                [(1, catalogs[0])],
            )


def _old_config(db: DatabaseWrapper) -> tuple[UUID, UUID]:
    state = MigrationExecutor(db).migrate([("identity", "0008_clinic_configuration")])
    org_id = uuid4()
    with transaction.atomic(using=db.alias), db.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant', %s, true)", [str(org_id)])
        org = state.apps.get_model("identity", "Organization").objects.create(
            id=org_id, name="Synthetic", cnpj="00000000000017"
        )
        clinic = state.apps.get_model("identity", "Clinic").objects.create(
            organization=org,
            name="Synthetic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        user = state.apps.get_model("identity", "User").objects.create(
            username="synthetic"
        )
        row = state.apps.get_model("identity", "ClinicConfiguration").objects.create(
            organization=org,
            clinic=clinic,
            published_by=user,
            version=1,
            display_name="Synthetic",
            reminder_hours=24,
        )
    return org_id, row.pk


def test_wrong_historical_hold_before_and_after_rewrite(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    target = ("ai", "0001_initial")
    schedule = ("identity", "0033_scheduling_policy")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        org, pk = _old_config(db)
        state = _predecessor(db, schedule)
        # Only the fixture's ADD COLUMN statement differs. The same old row
        # remains immutable, its SQL default is restored, and no record is faked.
        executor = MigrationExecutor(db)
        fixture = executor.loader.get_migration(*schedule)
        operations = [copy(op) for op in fixture.operations]
        (field,) = (
            op
            for op in operations
            if isinstance(op, migrations.AddField) and op.name == "hold_ttl_minutes"
        )
        field.field = field.field.clone()
        assert field.field.db_default == 10
        field.field.db_default = 17
        with patch.object(fixture, "operations", operations):
            executor.apply_migration(state, fixture)
        with db.cursor() as cur:
            cur.execute(
                "ALTER TABLE identity_clinicconfiguration "
                "ALTER hold_ttl_minutes SET DEFAULT 10"
            )
        _predecessor(db, target)
        for rewritten in (False, True):
            if rewritten:
                with db.cursor() as cur:
                    cur.execute("VACUUM FULL clinic_app.identity_clinicconfiguration")
            with transaction.atomic(using=db.alias), db.cursor() as cur:
                cur.execute(
                    "SELECT set_config('app.current_tenant',%s,true)", [str(org)]
                )
                cur.execute(
                    "SELECT version, hold_ttl_minutes "
                    "FROM identity_clinicconfiguration WHERE id=%s",
                    [pk],
                )
                assert cur.fetchone() == (1, 17)
            db.close()
            with pytest.raises(
                ResumeCatalogError, match=r"hold_ttl_minutes\.attmissingval"
            ):
                MigrationExecutor(db).migrate([target])
            db.close()
            assert target not in MigrationExecutor(db).loader.applied_migrations


@pytest.mark.parametrize("mode", ["valid", "legacy", "wrong-row", "lost-key"])
def test_reconnect_checks_the_physical_otp_cohort(
    mode: str, django_db_blocker: DjangoDbBlocker, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = ("identity", "0019_identity_lifecycle")
    identity = (".".join(target), "otp_static_staticdevice")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        state = _predecessor(db, ("otp_static", "0002_throttling"))
        user = state.apps.get_model("identity", "User").objects.create(
            username="cohort"
        )
        early = state.apps.get_model("otp_static", "StaticDevice").objects.create(
            user_id=user.pk, name="Early", confirmed=False
        )
        state = _predecessor(db, target)
        state.apps.get_model("otp_static", "StaticDevice").objects.create(
            user_id=user.pk, name="Later", confirmed=False, throttling_failure_count=8
        )
        executor = MigrationExecutor(db)

        def stop(migration: Migration) -> None:
            assert db.get_autocommit()
            assert not db.in_atomic_block
            raise SimulatedInterruptionError(migration.name)

        with monkeypatch.context() as cut:
            cut.setattr(executor, "record_migration", stop)
            with pytest.raises(SimulatedInterruptionError, match=target[1]):
                executor.apply_migration(state, executor.loader.get_migration(*target))
        db.close()
        with db.cursor() as cur:
            rows_query = (
                "SELECT id,created_at,throttling_failure_count "
                "FROM otp_static_staticdevice ORDER BY id"
            )
            cur.execute(rows_query)
            original = cur.fetchall()
            assert [row[2] for row in original] == [0, 8]
            cur.execute(
                "SELECT to_regclass('clinic_app.migration_default_witness') IS NOT NULL"
            )
            installed = cur.fetchone()[0]
            if mode == "valid":
                assert installed, "Committed DDL did not leave a durable witness"
                cur.execute(
                    "SELECT w.column_name,w.key_count,k.row_key=record_send(ROW(d.id)) "
                    "FROM migration_default_witness w "
                    "JOIN migration_default_witness_key k "
                    "USING(migration,relation_name,column_name) "
                    "CROSS JOIN otp_static_staticdevice d "
                    "WHERE (w.migration,w.relation_name)=(%s,%s) "
                    "AND d.id=%s ORDER BY 1",
                    [*identity, early.pk],
                )
                assert cur.fetchall() == [
                    ("created_at", 1, True),
                    ("throttling_failure_count", 1, True),
                ]
            elif mode in {"legacy", "lost-key"} and installed:
                cur.execute(
                    "DELETE FROM migration_default_witness_key "
                    "WHERE (migration,relation_name)=(%s,%s)",
                    identity,
                )
                if mode == "legacy":
                    cur.execute(
                        "DELETE FROM migration_default_witness "
                        "WHERE (migration,relation_name)=(%s,%s)",
                        identity,
                    )
            cur.execute("VACUUM FULL clinic_app.otp_static_staticdevice")
            if mode == "wrong-row":
                cur.execute(
                    "UPDATE otp_static_staticdevice "
                    "SET throttling_failure_count=17 WHERE id=%s",
                    [early.pk],
                )
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        if mode == "valid":
            MigrationExecutor(db).migrate([target])
            with db.cursor() as cur:
                cur.execute(rows_query)
                assert cur.fetchall() == original
            assert target in MigrationExecutor(db).loader.applied_migrations
        else:
            why = (
                "trusted migration witness is missing"
                if mode == "legacy"
                else "witnessed row"
            )
            with pytest.raises(
                ResumeCatalogError,
                match=why + r".*automatic recovery is impossible; restore or rebuild",
            ):
                MigrationExecutor(db).migrate([target])
            db.close()
            assert target not in MigrationExecutor(db).loader.applied_migrations


def test_witness_rolls_back_with_first_ddl(django_db_blocker: DjangoDbBlocker) -> None:
    target = ("identity", "0019_identity_lifecycle")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        state = _predecessor(db, target)
        executor = MigrationExecutor(db)

        def stop(
            run: Callable[..., object],
            query: object,
            params: object,
            many: bool,
            context: dict[str, object],
        ) -> object:
            result = run(query, params, many, context)
            if str(query).startswith('CREATE TABLE "identity_authenticatorreset"'):
                with db.cursor() as cur:
                    cur.execute(
                        "SELECT to_regclass('clinic_app.migration_default_witness')"
                        " IS NOT NULL"
                    )
                    assert cur.fetchone() == (True,), "No witness at first DDL"
                    cur.execute(
                        "SELECT count(*) FROM migration_default_witness "
                        "WHERE migration=%s "
                        "AND relation_name='otp_static_staticdevice'",
                        [".".join(target)],
                    )
                    assert cur.fetchone() == (2,)
                reason = "before first DDL commits"
                raise SimulatedInterruptionError(reason)
            return result

        with (
            db.execute_wrapper(stop),
            pytest.raises(SimulatedInterruptionError, match="before first DDL commits"),
        ):
            executor.apply_migration(state, executor.loader.get_migration(*target))
        db.close()
        assert target not in MigrationExecutor(db).loader.applied_migrations
        with db.cursor() as cur:
            cur.execute("SELECT to_regclass('clinic_app.identity_authenticatorreset')")
            assert cur.fetchone() == (None,)
            cur.execute(
                "SELECT count(*) FROM migration_default_witness WHERE migration=%s",
                [".".join(target)],
            )
            assert cur.fetchone() == (0,)


def test_runtime_cannot_read_write_or_alter_witness(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
        db.cursor() as cur,
    ):
        for table in ("migration_default_witness", "migration_default_witness_key"):
            cur.execute("SELECT to_regclass(%s) IS NOT NULL", [f"clinic_app.{table}"])
            assert cur.fetchone() == (True,), "Witness storage does not exist"
            with transaction.atomic(using=db.alias):
                cur.execute("SET LOCAL ROLE clinic_app")
                for command in (
                    "SELECT * FROM {}",
                    "INSERT INTO {} DEFAULT VALUES",
                    "UPDATE {} SET migration=migration",
                    "DELETE FROM {}",
                    "TRUNCATE {}",
                    "ALTER TABLE {} ADD rogue int",
                ):
                    with (
                        pytest.raises(DatabaseError) as error,
                        transaction.atomic(using=db.alias),
                    ):
                        cur.execute(
                            sql.SQL(command).format(sql.Identifier("clinic_app", table))
                        )
                    assert getattr(error.value.__cause__, "sqlstate", None) == "42501"

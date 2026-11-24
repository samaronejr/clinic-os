"""Populated forward recovery through the next migration, not just its recorder."""

from __future__ import annotations

import os
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING
from unittest.mock import patch
from uuid import uuid4

import pytest
from django.db import transaction
from django.db.migrations.executor import MigrationExecutor
from psycopg import sql

from infra.test_migration_relationship_scope import _predecessor
from infra.test_migration_witness import _old_config
from infra.test_protected_migration_resume import SimulatedInterruptionError
from patients.test_intake_migrations import (
    _default_connection,
    _scratch_database,
    _scratch_tenant_key,
)

if TYPE_CHECKING:
    from uuid import UUID

    from django.db.backends.postgresql.base import DatabaseWrapper
    from django.db.migrations import Migration
    from django.db.migrations.state import ProjectState
    from pytest_django.plugin import DjangoDbBlocker


def _cut(db: DatabaseWrapper, target: tuple[str, str], state: ProjectState) -> None:
    executor = MigrationExecutor(db)

    def stop(migration: Migration) -> None:
        assert (migration.app_label, migration.name) == target
        assert db.get_autocommit()
        assert not db.in_atomic_block
        raise SimulatedInterruptionError(migration.name)

    with (
        patch.object(executor, "record_migration", stop),
        pytest.raises(SimulatedInterruptionError, match=target[1]),
    ):
        executor.apply_migration(state, executor.loader.get_migration(*target))
    db.close()
    assert target not in MigrationExecutor(db).loader.applied_migrations


def _to_head(db: DatabaseWrapper) -> None:
    db.close()
    ex = MigrationExecutor(db)
    leaves = ex.loader.graph.leaf_nodes()
    ex.migrate(leaves)
    db.close()
    assert MigrationExecutor(db).migration_plan(leaves) == []


def _head_predecessor(db: DatabaseWrapper, target: tuple[str, str]) -> ProjectState:
    """Stop the actual migrate-to-leaves plan immediately before target."""
    ex = MigrationExecutor(db)
    leaves = ex.loader.graph.leaf_nodes()
    plan = ex.migration_plan(leaves)
    stop = next(
        i
        for i, (migration, backwards) in enumerate(plan)
        if not backwards and (migration.app_label, migration.name) == target
    )
    return ex.migrate(leaves, plan=plan[:stop])


def test_verified_hold_survives_later_protected_migrations(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    target = ("ai", "0001_initial")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        org, pk = _old_config(db)
        _cut(db, target, _predecessor(db, target))
        with db.cursor() as cur:
            cur.execute(
                "SELECT key_count FROM migration_default_witness WHERE migration=%s "
                "AND relation_name='identity_clinicconfiguration' "
                "AND column_name='hold_ttl_minutes'",
                [".".join(target)],
            )
            assert cur.fetchone() == (1,)
            cur.execute("VACUUM FULL clinic_app.identity_clinicconfiguration")
        MigrationExecutor(db).migrate([target])
        db.close()
        assert target in MigrationExecutor(db).loader.applied_migrations
        # The published parent fails here even though the original key reads 10.
        _to_head(db)
        with transaction.atomic(using=db.alias), db.cursor() as cur:
            cur.execute("SELECT set_config('app.current_tenant',%s,true)", [str(org)])
            cur.execute(
                "SELECT hold_ttl_minutes FROM identity_clinicconfiguration WHERE id=%s",
                [pk],
            )
            assert cur.fetchone() == (10,)


def _seed_encounter(db: DatabaseWrapper) -> tuple[UUID, UUID, UUID]:
    apps = MigrationExecutor(db).migrate([("ehr", "0001_initial")]).apps
    org_id, patient_id, encounter_id = uuid4(), uuid4(), uuid4()
    start = datetime(2035, 6, 2, 11, 0, tzinfo=UTC)
    with transaction.atomic(using=db.alias), db.cursor() as cur:
        cur.execute("SELECT set_config('app.current_tenant',%s,true)", [str(org_id)])
        org = apps.get_model("identity", "Organization").objects.create(
            id=org_id, name="Synthetic", cnpj="00000000008001"
        )
        clinic = apps.get_model("identity", "Clinic").objects.create(
            organization=org,
            name="Synthetic",
            crm_uf="SP",
            timezone="America/Sao_Paulo",
        )
        user = apps.get_model("identity", "User").objects.create(
            username="r8.synthetic"
        )
        apps.get_model("identity", "UserClinicRole").objects.create(
            organization=org, clinic=clinic, user=user, role="physician"
        )
        patient = apps.get_model("intake", "Patient").objects.create(
            id=patient_id,
            organization=org,
            full_name="Synthetic Fixture",
            birth_date=date(1990, 1, 1),
        )
        apps.get_model("intake", "PatientClinicEnrollment").objects.create(
            organization=org,
            clinic=clinic,
            patient=patient,
            idempotency_key=uuid4(),
            create_fingerprint=b"e" * 32,
        )
        apps.get_model("scheduling", "AvailabilityBlock").objects.create(
            organization=org,
            clinic=clinic,
            practitioner=user,
            start_at=start,
            end_at=start + timedelta(hours=3),
            idempotency_key=uuid4(),
            create_fingerprint=b"b" * 32,
        )
        appointment = apps.get_model("scheduling", "Appointment").objects.create(
            organization=org,
            clinic=clinic,
            patient=patient,
            practitioner=user,
            start_at=start + timedelta(minutes=15),
            end_at=start + timedelta(minutes=45),
            idempotency_key=uuid4(),
            create_fingerprint=b"a" * 32,
        )
        apps.get_model("ehr", "Encounter").objects.create(
            id=encounter_id,
            organization=org,
            clinic=clinic,
            physician=user,
            appointment=appointment,
            patient=patient,
        )
    # The historical patient predates encrypted fields. Provision its tenant
    # key before advancing through the real protected-field backfill.
    keys = ("tenancy", "0003_tenant_envelope")
    executor = MigrationExecutor(db)
    if keys not in executor.loader.applied_migrations:
        executor.migrate([keys])
    _scratch_tenant_key(db, org_id)
    return org_id, patient_id, encounter_id


def _parent_triggers(db: DatabaseWrapper) -> set[int]:
    with db.cursor() as cur:
        cur.execute(
            "SELECT oid FROM pg_trigger WHERE tgisinternal "
            "AND tgrelid='ehr_encounter'::regclass"
        )
        return {oid for (oid,) in cur.fetchall()}


def test_recreated_fk_triggers_require_encounter_witness(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    target = ("prescription", "0012_clinical_document_kinds")
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        org, _, pk = _seed_encounter(db)
        # Isolate the FK-parent control from the independent index horizon bug.
        _predecessor(db, ("intake", "0013_patient_demographics"))
        with db.cursor() as cur:
            cur.execute(
                "REINDEX INDEX clinic_app.intake_patient_full_name_index_fe3240b2"
            )
        # The interrupted invocation and its retry both target all leaves.
        # Applying only target's ancestors, then switching to a different
        # plan after the cut, could run an independent branch ahead of the
        # unrecorded DDL and would not be a retry of the original upgrade.
        state = _head_predecessor(db, target)
        old = _parent_triggers(db)
        _cut(db, target, state)
        assert len(old - _parent_triggers(db)) == 2
        with db.cursor() as cur:
            cur.execute(
                "SELECT key_count FROM migration_default_witness "
                "WHERE (migration,relation_name,column_name)=(%s,%s,%s)",
                [".".join(target), "ehr_encounter", "unscheduled_reason"],
            )
            assert cur.fetchone() == (1,), (
                "replaced FK triggers did not witness the parent"
            )
            cur.execute("VACUUM FULL clinic_app.ehr_encounter")
        _to_head(db)
        with transaction.atomic(using=db.alias), db.cursor() as cur:
            cur.execute("SELECT set_config('app.current_tenant',%s,true)", [str(org)])
            cur.execute(
                "SELECT unscheduled_reason FROM ehr_encounter WHERE id=%s", [pk]
            )
            assert cur.fetchone() == ("",)


def test_populated_patient_index_snapshot_horizon_is_not_schema(
    django_db_blocker: DjangoDbBlocker,
) -> None:
    target = ("intake", "0013_patient_demographics")
    index = "intake_patient_full_name_index_fe3240b2"
    with (
        django_db_blocker.unblock(),
        _scratch_database(os.environ["TEST_SUPERUSER_DATABASE_URL"]) as db,
        _default_connection(db),
    ):
        org, patient, _ = _seed_encounter(db)
        _predecessor(db, target)
        with transaction.atomic(using=db.alias), db.cursor() as cur:
            cur.execute("SELECT set_config('app.current_tenant',%s,true)", [str(org)])
            cur.execute(
                "SELECT indcheckxmin,indisvalid,indisready,indislive "
                "FROM pg_index WHERE indexrelid=%s::regclass",
                [index],
            )
            assert cur.fetchone() == (True, True, True, True)
            cur.execute(
                "SELECT full_name_index FROM intake_patient WHERE id=%s", [patient]
            )
            (digest,) = cur.fetchone()
            cur.execute("SET LOCAL enable_seqscan=off")
            query = "SELECT id FROM intake_patient WHERE full_name_index=%s"
            cur.execute("EXPLAIN (ANALYZE, FORMAT JSON) " + query, [digest])
            assert index in str(cur.fetchone())
            cur.execute(query, [digest])
            assert cur.fetchall() == [(patient,)]
        db.close()
        MigrationExecutor(db).migrate([target])
        with db.cursor() as cur:
            cur.execute(
                sql.SQL("REINDEX INDEX {}").format(sql.Identifier("clinic_app", index))
            )
            cur.execute(
                "SELECT indcheckxmin,indisvalid,indisready,indislive "
                "FROM pg_index WHERE indexrelid=%s::regclass",
                [index],
            )
            assert cur.fetchone() == (False, True, True, True)
        _to_head(db)

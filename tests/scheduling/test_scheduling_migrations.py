from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from importlib import import_module
from importlib.util import find_spec
from queue import Queue
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import UUID

import pytest
from django.db import IntegrityError, connection, transaction
from django.db.migrations.executor import MigrationExecutor

from scheduling.availability_test_support import (
    CLINIC_A_ID,
    CLINIC_B_ID,
    END,
    PRACTITIONER_A_ID,
    START,
    race_insert,
    seed,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

MIGRATION_MODULE = "apps.scheduling.migrations.0001_availability_block"
MIGRATION_DEPENDENCIES = [
    ("identity", "0005_clinic_timezone"),
    ("intake", "0001_patient_and_enrollment"),
]


def _require_migration() -> None:
    assert find_spec(MIGRATION_MODULE) is not None
    module = import_module(MIGRATION_MODULE)
    assert module.Migration.dependencies == MIGRATION_DEPENDENCIES


@pytest.mark.django_db(transaction=True)
def test_availability_migration_installs_immediate_active_overlap_integrity() -> None:
    _require_migration()
    models = import_module("apps.scheduling.models")
    availability = models.AvailabilityBlock
    organization, clinic_a, clinic_b, practitioner_a, practitioner_b = seed()
    first = availability.objects.create(
        organization=organization,
        clinic=clinic_a,
        practitioner=practitioner_a,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=5301),
        create_fingerprint=b"a" * 32,
    )
    availability.objects.create(
        organization=organization,
        clinic=clinic_b,
        practitioner=practitioner_a,
        start_at=END,
        end_at=END + timedelta(hours=1),
        idempotency_key=UUID(int=5302),
        create_fingerprint=b"b" * 32,
    )
    availability.objects.create(
        organization=organization,
        clinic=clinic_b,
        practitioner=practitioner_b,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=5303),
        create_fingerprint=b"c" * 32,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        availability.objects.create(
            organization=organization,
            clinic=clinic_b,
            practitioner=practitioner_a,
            start_at=START + timedelta(minutes=30),
            end_at=END + timedelta(minutes=30),
            idempotency_key=UUID(int=5304),
            create_fingerprint=b"d" * 32,
        )
    availability.objects.filter(pk=first.pk).update(retired_at=datetime.now(UTC))
    availability.objects.create(
        organization=organization,
        clinic=clinic_b,
        practitioner=practitioner_a,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=5305),
        create_fingerprint=b"e" * 32,
    )
    invalid_rows = (
        {"start_at": START, "end_at": START, "create_fingerprint": b"f" * 32},
        {
            "start_at": START + timedelta(seconds=1),
            "end_at": END,
            "create_fingerprint": b"g" * 32,
        },
        {
            "start_at": END + timedelta(hours=4),
            "end_at": END + timedelta(hours=5),
            "create_fingerprint": b"short",
        },
        {
            "start_at": END + timedelta(hours=2),
            "end_at": END + timedelta(hours=3),
            "create_fingerprint": b"h" * 32,
            "retired_at": datetime(2020, 1, 1, tzinfo=UTC),
        },
    )
    for offset, values in enumerate(invalid_rows, start=1):
        with pytest.raises(IntegrityError), transaction.atomic():
            availability.objects.create(
                organization=organization,
                clinic=clinic_a,
                practitioner=practitioner_b,
                idempotency_key=UUID(int=5400 + offset),
                **values,
            )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT extname FROM pg_catalog.pg_extension WHERE extname = 'btree_gist'"
        )
        assert cursor.fetchall() == [("btree_gist",)]
        cursor.execute(
            "SELECT conname, contype, condeferrable, condeferred "
            "FROM pg_catalog.pg_constraint "
            "WHERE conrelid = 'clinic_app.scheduling_availabilityblock'::regclass "
            "AND conname = ANY(%s) ORDER BY conname",
            [
                [
                    "scheduling_availability_active_practitioner_excl",
                    "scheduling_availability_fingerprint_32_check",
                    "scheduling_availability_org_clinic_fk",
                    "scheduling_availability_positive_minute_range_check",
                    "scheduling_availability_retirement_check",
                ]
            ],
        )
        rows = cursor.fetchall()
        assert [row[0] for row in rows] == sorted(row[0] for row in rows)
        exclusion = next(row for row in rows if row[1] == "x")
        assert exclusion[2:] == (False, False)
        cursor.execute(
            "SELECT indexname FROM pg_catalog.pg_indexes "
            "WHERE schemaname = 'clinic_app' AND tablename = "
            "'scheduling_availabilityblock' AND indexname = ANY(%s) "
            "ORDER BY indexname",
            [
                [
                    "scheduling_availability_clinic_start_idx",
                    "scheduling_availability_practitioner_start_idx",
                ]
            ],
        )
        assert cursor.fetchall() == [
            ("scheduling_availability_clinic_start_idx",),
            ("scheduling_availability_practitioner_start_idx",),
        ]


@pytest.mark.django_db(transaction=True)
def test_concurrent_overlapping_availability_has_one_winner(
    app_database_url: str,
) -> None:
    _require_migration()
    seed()
    barrier = Barrier(2)
    results: Queue[str] = Queue()
    calls = (
        (UUID(int=5501), CLINIC_A_ID, UUID(int=5601)),
        (UUID(int=5502), CLINIC_B_ID, UUID(int=5602)),
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(
                race_insert,
                app_database_url,
                barrier,
                (row_id, clinic_id, key),
                results,
            )
            for row_id, clinic_id, key in calls
        ]
        for future in futures:
            future.result(timeout=8)
    assert sorted(results.get_nowait() for _ in calls) == ["conflict", "inserted"]
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT count(*) FROM clinic_app.scheduling_availabilityblock "
            "WHERE practitioner_id = %s",
            [PRACTITIONER_A_ID],
        )
        assert cursor.fetchone() == (1,)


def _migrate(targets: Sequence[tuple[str, str | None]]) -> None:
    MigrationExecutor(connection).migrate(targets)


@pytest.mark.django_db(transaction=True)
def test_availability_migration_reverses_and_reapplies() -> None:
    _require_migration()
    try:
        _migrate([("scheduling", None)])
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.to_regclass("
                "'clinic_app.scheduling_availabilityblock')"
            )
            assert cursor.fetchone() == (None,)
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.to_regclass("
                "'clinic_app.scheduling_availabilityblock')"
            )
            assert cursor.fetchone()[0] is not None
    finally:
        executor = MigrationExecutor(connection)
        executor.migrate(executor.loader.graph.leaf_nodes())

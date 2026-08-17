from __future__ import annotations

from datetime import date
from queue import Queue
from threading import Barrier, Thread
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.intake.patient_creation import (
    PatientIdempotencyConflictError,
    create_patient,
)
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.tenancy.db import tenant_context
from django.db import close_old_connections, connection, connections, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

type RaceOutcome = tuple[UUID, UUID] | Exception


def test_cross_clinic_same_key_race_rolls_back_loser_and_returns_conflict(
    rbac_graph: RbacGraph,
) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.RECEPTIONIST,
        )

    barrier = Barrier(2)
    outcomes: Queue[RaceOutcome] = Queue()
    idempotency_key = uuid4()

    def worker(clinic_id: UUID) -> None:
        close_old_connections()
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
            ):
                acquire_advisory_locks((clinic_lock_key(clinic_id),))
                barrier.wait(timeout=10)
                registration = create_patient(
                    clinic_id=clinic_id,
                    full_name="Ana Synthetic",
                    birth_date=date(2000, 1, 2),
                    idempotency_key=idempotency_key,
                )
                outcomes.put((registration.patient.pk, registration.enrollment.pk))
        except PatientIdempotencyConflictError as error:
            outcomes.put(error)
        finally:
            connections.close_all()

    threads = [
        Thread(target=worker, args=(clinic_id,))
        for clinic_id in (rbac_graph.clinic_a, rbac_graph.clinic_b)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert sum(isinstance(result, tuple) for result in results) == 1
    assert (
        sum(isinstance(result, PatientIdempotencyConflictError) for result in results)
        == 1
    )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT count(*) FROM clinic_app.intake_patientclinicenrollment")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        assert cursor.fetchone() == (1,)


def test_same_clinic_equal_key_race_returns_one_registration_without_new_audit(
    rbac_graph: RbacGraph,
) -> None:
    barrier = Barrier(2)
    outcomes: Queue[RaceOutcome] = Queue()
    idempotency_key = uuid4()

    def worker() -> None:
        close_old_connections()
        try:
            with (
                runtime_role(),
                tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
            ):
                barrier.wait(timeout=10)
                registration = create_patient(
                    clinic_id=rbac_graph.clinic_a,
                    full_name="Ana Synthetic",
                    birth_date=date(2000, 1, 2),
                    idempotency_key=idempotency_key,
                )
                outcomes.put((registration.patient.pk, registration.enrollment.pk))
        finally:
            connections.close_all()

    threads = [Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    results = [outcomes.get_nowait(), outcomes.get_nowait()]
    assert all(isinstance(result, tuple) for result in results)
    assert results[0] == results[1]
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        cursor.execute("SELECT count(*) FROM clinic_app.intake_patient")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT count(*) FROM clinic_app.intake_patientclinicenrollment")
        assert cursor.fetchone() == (1,)
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        assert cursor.fetchone() == (1,)

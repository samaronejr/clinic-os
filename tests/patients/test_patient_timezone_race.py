from __future__ import annotations

from datetime import UTC, date, datetime
from queue import Queue
from threading import Barrier, Thread
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic
from apps.intake import patient_creation
from apps.intake.models import Patient
from apps.intake.patient_creation import PatientBirthDateError
from apps.scheduling.locks import acquire_advisory_locks, clinic_lock_key
from apps.scheduling.timezones import (
    ClinicTimezoneLockedError,
    ensure_clinic_timezone_change_allowed,
)
from apps.tenancy.db import tenant_context
from django.db import close_old_connections, connection, connections, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)

type TimezoneRaceOutcome = str | tuple[str, UUID] | Exception


def _set_initial_timezone(rbac_graph: RbacGraph) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        Clinic.objects.filter(pk=rbac_graph.clinic_a).update(
            timezone="Pacific/Kiritimati"
        )


def _create_worker(
    rbac_graph: RbacGraph,
    barrier: Barrier,
    outcomes: Queue[TimezoneRaceOutcome],
) -> None:
    close_old_connections()
    try:
        with (
            runtime_role(),
            tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        ):
            barrier.wait(timeout=10)
            registration = patient_creation.create_patient(
                clinic_id=rbac_graph.clinic_a,
                full_name="Ana Synthetic",
                birth_date=date(2030, 1, 2),
                idempotency_key=uuid4(),
            )
            outcomes.put(("created", registration.patient.pk))
    except PatientBirthDateError as error:
        outcomes.put(error)
    finally:
        connections.close_all()


def _timezone_worker(
    rbac_graph: RbacGraph,
    barrier: Barrier,
    outcomes: Queue[TimezoneRaceOutcome],
) -> None:
    close_old_connections()
    try:
        with transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [str(rbac_graph.organization_a)],
            )
            barrier.wait(timeout=10)
            acquire_advisory_locks((clinic_lock_key(rbac_graph.clinic_a),))
            ensure_clinic_timezone_change_allowed(rbac_graph.clinic_a)
            Clinic.objects.filter(pk=rbac_graph.clinic_a).update(
                timezone="Pacific/Honolulu"
            )
            outcomes.put("timezone_changed")
    except ClinicTimezoneLockedError as error:
        outcomes.put(error)
    finally:
        connections.close_all()


def _run_race(rbac_graph: RbacGraph) -> list[TimezoneRaceOutcome]:
    barrier = Barrier(2)
    outcomes: Queue[TimezoneRaceOutcome] = Queue()
    threads = [
        Thread(target=_create_worker, args=(rbac_graph, barrier, outcomes)),
        Thread(target=_timezone_worker, args=(rbac_graph, barrier, outcomes)),
    ]

    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)

    assert all(not thread.is_alive() for thread in threads)
    return [outcomes.get_nowait(), outcomes.get_nowait()]


def test_patient_create_and_timezone_change_serialize_on_clinic_date(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2030, 1, 2, 2, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "apps.intake.patient_creation.timezone.now",
        lambda: fixed_now,
    )
    _set_initial_timezone(rbac_graph)
    results = _run_race(rbac_graph)
    create_won = any(isinstance(result, tuple) for result in results)
    timezone_won = "timezone_changed" in results
    assert create_won != timezone_won
    if create_won:
        assert any(isinstance(result, ClinicTimezoneLockedError) for result in results)
        expected_timezone = "Pacific/Kiritimati"
        expected_patient_count = 1
    else:
        assert any(isinstance(result, PatientBirthDateError) for result in results)
        expected_timezone = "Pacific/Honolulu"
        expected_patient_count = 0

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
        connection.cursor() as cursor,
    ):
        clinic = Clinic.objects.get(pk=rbac_graph.clinic_a)
        patient_dates = list(Patient.objects.values_list("birth_date", flat=True))
        cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
        audit_count = cursor.fetchone()

    assert clinic.timezone == expected_timezone
    assert len(patient_dates) == expected_patient_count
    assert all(
        patient_date is not None and patient_date <= date(2030, 1, 2)
        for patient_date in patient_dates
    )
    assert audit_count == (expected_patient_count,)

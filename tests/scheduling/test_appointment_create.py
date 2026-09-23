from __future__ import annotations

from datetime import UTC, date, datetime
from inspect import Parameter, signature
from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.core.idempotency import create_fingerprint
from apps.intake.services import create_patient
from apps.scheduling.models import Appointment
from apps.scheduling.services import SlotConflict, create_appointment
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_concurrency_support import (
    start_appointment_creator,
    wait_for_row_waiter,
)
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_public_create_appointment_derives_context_from_keyword_only_inputs() -> None:
    parameters = signature(create_appointment).parameters
    assert list(parameters) == [
        "clinic_id",
        "enrollment_id",
        "practitioner_id",
        "local_range",
        "idempotency_key",
    ]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())
    assert not {
        "actor",
        "actor_id",
        "organization",
        "organization_id",
        "status",
    }.intersection(parameters)


def test_receptionist_creates_one_fingerprinted_booking_and_exact_audit(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        appointment = create_synthetic_appointment(setup, idempotency_key=key)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            audit_rows = cursor.fetchall()

    assert appointment.organization_id == setup.organization_id
    assert appointment.clinic_id == setup.clinic_id
    assert appointment.patient_id == setup.patient_id
    assert appointment.practitioner_id == setup.practitioner_id
    assert appointment.start_at == datetime(2035, 6, 2, 12, 0, tzinfo=UTC)
    assert appointment.end_at == datetime(2035, 6, 2, 13, 0, tzinfo=UTC)
    assert appointment.idempotency_key == key
    assert appointment.status == Appointment.Status.SCHEDULED
    assert bytes(appointment.create_fingerprint) == create_fingerprint(
        "appointment",
        {
            "clinic_id": str(setup.clinic_id),
            "end_utc": "2035-06-02T13:00:00Z",
            "enrollment_id": str(setup.enrollment_id),
            "practitioner_id": str(setup.practitioner_id),
            "start_utc": "2035-06-02T12:00:00Z",
        },
    )
    assert audit_rows == [
        (
            "scheduling.appointment.created",
            "scheduling.appointment",
            str(appointment.pk),
            2,
            str(setup.clinic_id),
            "created",
        )
    ]


def test_named_practitioner_exclusion_maps_to_opaque_slot_conflict(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        other = create_patient(
            clinic_id=setup.clinic_id,
            full_name="Synthetic Concurrent Booking Persona",
            birth_date=date(2001, 2, 3),
            idempotency_key=uuid4(),
        )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        Appointment.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            patient_id=other.patient.pk,
            practitioner_id=setup.practitioner_id,
            start_at=datetime(2035, 6, 2, 12, 0, tzinfo=UTC),
            end_at=datetime(2035, 6, 2, 13, 0, tzinfo=UTC),
            idempotency_key=uuid4(),
            create_fingerprint=b"p" * 32,
        )
        thread, backend_pids, outcomes = start_appointment_creator(setup, uuid4())
        wait_for_row_waiter(backend_pids.get(timeout=5), outcomes)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcomes.get_nowait(), SlotConflict)


def test_named_patient_exclusion_maps_to_opaque_slot_conflict(
    rbac_graph: RbacGraph,
) -> None:
    first, second = seed_cross_clinic_appointment_setups(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(first.organization_id)],
        )
        Appointment.objects.create(
            organization_id=second.organization_id,
            clinic_id=second.clinic_id,
            patient_id=second.patient_id,
            practitioner_id=second.practitioner_id,
            start_at=datetime(2035, 6, 2, 12, 0, tzinfo=UTC),
            end_at=datetime(2035, 6, 2, 13, 0, tzinfo=UTC),
            idempotency_key=uuid4(),
            create_fingerprint=b"q" * 32,
        )
        thread, backend_pids, outcomes = start_appointment_creator(first, uuid4())
        wait_for_row_waiter(backend_pids.get(timeout=5), outcomes)
    thread.join(timeout=10)

    assert not thread.is_alive()
    assert isinstance(outcomes.get_nowait(), SlotConflict)


def test_adjacent_half_open_slots_are_both_accepted(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        first = create_synthetic_appointment(setup)
        adjacent = create_synthetic_appointment(
            setup,
            start_local="2035-06-02T10:00",
            end_local="2035-06-02T11:00",
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            created_events = cursor.fetchone()

    assert first.end_at == adjacent.start_at
    assert created_events == (2,)


def test_unrelated_integrity_error_is_not_translated_or_committed(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with connection.cursor() as cursor:
        cursor.execute(
            "ALTER TABLE clinic_app.scheduling_appointment "
            "ADD CONSTRAINT todo10_unrelated_check CHECK (id <> id) NOT VALID"
        )
    try:
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            with pytest.raises(IntegrityError) as error_info:
                create_synthetic_appointment(setup)
            cause = error_info.value.__cause__
            assert isinstance(cause, psycopg.Error)
            assert cause.diag.constraint_name == "todo10_unrelated_check"
            assert Appointment.objects.count() == 0
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT count(*) FROM clinic_app.audit_event_tenant "
                    "WHERE event_type = 'scheduling.appointment.created'"
                )
                assert cursor.fetchone() == (0,)
    finally:
        with connection.cursor() as cursor:
            cursor.execute(
                "ALTER TABLE clinic_app.scheduling_appointment "
                "DROP CONSTRAINT todo10_unrelated_check"
            )

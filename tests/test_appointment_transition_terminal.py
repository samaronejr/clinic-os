from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import psycopg
import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentAvailabilityError,
    AppointmentCancellationConflictError,
    AppointmentIdempotencyConflictError,
    AppointmentLocalRange,
    AppointmentTerminalError,
    cancel_appointment,
    reschedule_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection, transaction

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_terminal_guards_and_create_replay_remain_exact_after_role_removal(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    original_key = uuid4()
    reusable_key = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        original = create_synthetic_appointment(
            setup,
            idempotency_key=original_key,
        )
        with pytest.raises(AppointmentAvailabilityError):
            create_synthetic_appointment(
                setup,
                idempotency_key=reusable_key,
                start_local="2035-06-02T07:00",
                end_local="2035-06-02T08:00",
            )
        _ = cancel_appointment(
            appointment_id=original.pk,
            reason=Appointment.CancellationReason.CLINIC_REQUEST,
        )
        with pytest.raises(AppointmentCancellationConflictError):
            cancel_appointment(
                appointment_id=original.pk,
                reason=Appointment.CancellationReason.OTHER,
            )
        with pytest.raises(AppointmentTerminalError):
            reschedule_appointment(
                appointment_id=original.pk,
                local_range=AppointmentLocalRange(
                    start_local="2035-06-02T10:00",
                    end_local="2035-06-02T11:00",
                ),
            )
        accepted = create_synthetic_appointment(
            setup,
            idempotency_key=reusable_key,
        )
        with (
            pytest.raises(IntegrityError) as database_terminal_error,
            transaction.atomic(),
        ):
            Appointment.objects.filter(pk=original.pk).update(
                cancellation_reason=Appointment.CancellationReason.OTHER,
            )
        cause = database_terminal_error.value.__cause__
        assert isinstance(cause, psycopg.Error)
        assert cause.diag.constraint_name == "scheduling_appointment_terminal_check"

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        UserClinicRole.objects.filter(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user_id=setup.practitioner_id,
            role=UserClinicRole.Role.PHYSICIAN,
        ).delete()

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        replay = create_synthetic_appointment(
            setup,
            idempotency_key=original_key,
        )
        with pytest.raises(AppointmentIdempotencyConflictError):
            create_synthetic_appointment(
                setup,
                idempotency_key=original_key,
                start_local="2035-06-02T09:15",
            )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type IN ("
                "'scheduling.appointment.created', "
                "'scheduling.appointment.rescheduled', "
                "'scheduling.appointment.cancelled') GROUP BY event_type "
                "ORDER BY event_type"
            )
            event_counts = cursor.fetchall()

    assert accepted.idempotency_key == reusable_key
    assert replay.pk == original.pk
    assert replay.status == Appointment.Status.CANCELLED
    assert event_counts == [
        ("scheduling.appointment.cancelled", 1),
        ("scheduling.appointment.created", 2),
    ]

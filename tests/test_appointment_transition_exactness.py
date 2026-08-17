from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentLocalRange,
    cancel_appointment,
    reschedule_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import connection

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _state(appointment: Appointment) -> tuple[object, ...]:
    return (
        appointment.start_at,
        appointment.end_at,
        appointment.status,
        appointment.cancellation_reason,
        appointment.cancelled_at,
        appointment.updated_at,
        bytes(appointment.create_fingerprint),
    )


def test_identical_reschedule_and_equal_recancel_are_exact_noops(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        before_reschedule = _state(appointment)
        unchanged = reschedule_appointment(
            appointment_id=appointment.pk,
            local_range=AppointmentLocalRange(
                start_local="2035-06-02T09:00",
                end_local="2035-06-02T10:00",
            ),
        )
        after_reschedule = _state(unchanged)
        cancelled = cancel_appointment(
            appointment_id=appointment.pk,
            reason=Appointment.CancellationReason.PATIENT_REQUEST,
        )
        before_recancel = _state(cancelled)
        still_cancelled = cancel_appointment(
            appointment_id=appointment.pk,
            reason=Appointment.CancellationReason.PATIENT_REQUEST,
        )
        after_recancel = _state(still_cancelled)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type IN ("
                "'scheduling.appointment.rescheduled', "
                "'scheduling.appointment.cancelled') GROUP BY event_type"
            )
            transition_events = cursor.fetchall()

    assert before_reschedule == after_reschedule
    assert before_recancel == after_recancel
    assert transition_events == [("scheduling.appointment.cancelled", 1)]

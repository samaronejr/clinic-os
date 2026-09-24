from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.services import record_phase1_event, verify_chain
from apps.scheduling import patient_authority
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentCancellationInputError,
    AppointmentLocalRange,
    AppointmentRescheduleInputError,
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


class ForcedTransitionAuditFailureError(Exception):
    pass


def test_invalid_inputs_and_failed_audits_leave_transition_state_reusable(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    original_append = record_phase1_event

    def append_then_fail(
        event_type: str,
        *,
        clinic_id: UUID,
        affected_record_id: UUID,
    ) -> int:
        original_append(
            event_type,
            clinic_id=clinic_id,
            affected_record_id=affected_record_id,
        )
        raise ForcedTransitionAuditFailureError

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        original_range = (appointment.start_at, appointment.end_at)
        with pytest.raises(AppointmentCancellationInputError):
            cancel_appointment(appointment_id=appointment.pk, reason="free form")
        with pytest.raises(AppointmentRescheduleInputError):
            reschedule_appointment(
                appointment_id=appointment.pk,
                local_range=AppointmentLocalRange(
                    start_local="2035-06-02 10:00",
                    end_local="2035-06-02T11:00",
                ),
            )
        with pytest.raises(AppointmentAccessDeniedError):
            cancel_appointment(
                appointment_id=uuid4(),
                reason=Appointment.CancellationReason.OTHER,
            )

        monkeypatch.setattr(
            patient_authority,
            "record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedTransitionAuditFailureError):
            reschedule_appointment(
                appointment_id=appointment.pk,
                local_range=AppointmentLocalRange(
                    start_local="2035-06-02T10:00",
                    end_local="2035-06-02T11:00",
                ),
            )
        appointment.refresh_from_db()
        assert (appointment.start_at, appointment.end_at) == original_range
        monkeypatch.setattr(
            patient_authority,
            "record_phase1_event",
            original_append,
        )
        _ = reschedule_appointment(
            appointment_id=appointment.pk,
            local_range=AppointmentLocalRange(
                start_local="2035-06-02T10:00",
                end_local="2035-06-02T11:00",
            ),
        )

        monkeypatch.setattr(
            patient_authority,
            "record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedTransitionAuditFailureError):
            cancel_appointment(
                appointment_id=appointment.pk,
                reason=Appointment.CancellationReason.OTHER,
            )
        appointment.refresh_from_db()
        assert appointment.status == Appointment.Status.SCHEDULED
        monkeypatch.setattr(
            patient_authority,
            "record_phase1_event",
            original_append,
        )
        _ = cancel_appointment(
            appointment_id=appointment.pk,
            reason=Appointment.CancellationReason.OTHER,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type IN ("
                "'scheduling.appointment.rescheduled', "
                "'scheduling.appointment.cancelled') GROUP BY event_type "
                "ORDER BY event_type"
            )
            event_counts = cursor.fetchall()
        verification = verify_chain(setup.organization_id)

    assert event_counts == [
        ("scheduling.appointment.cancelled", 1),
        ("scheduling.appointment.rescheduled", 1),
    ]
    assert verification.valid is True

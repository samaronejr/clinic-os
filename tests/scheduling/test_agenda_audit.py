from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.services import record_phase1_event, verify_chain
from apps.scheduling import agenda_queries
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    prepare_booking,
    view_agenda,
    view_appointment_for_transition,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class ForcedAgendaAuditFailureError(Exception):
    pass


def test_read_events_are_exact_once_transactional_and_denials_are_silent(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        prepare_booking(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
        )
        view_appointment_for_transition(appointment_id=appointment.pk)
        empty = view_agenda(
            clinic_id=setup.clinic_id,
            view="day",
            date="2035-06-03",
            page=1,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type IN ("
                "'scheduling.booking.viewed', "
                "'scheduling.appointment.viewed', "
                "'scheduling.agenda.viewed') ORDER BY seq"
            )
            view_events = cursor.fetchall()

    assert empty.items == ()
    assert view_events == [
        (
            "scheduling.booking.viewed",
            "intake.patient_clinic_enrollment",
            str(setup.enrollment_id),
            2,
            str(setup.clinic_id),
            "viewed",
        ),
        (
            "scheduling.appointment.viewed",
            "scheduling.appointment",
            str(appointment.pk),
            2,
            str(setup.clinic_id),
            "viewed",
        ),
        (
            "scheduling.agenda.viewed",
            "identity.clinic",
            str(setup.clinic_id),
            2,
            str(setup.clinic_id),
            "viewed",
        ),
    ]

    with runtime_role(), tenant_context(setup.practitioner_id, setup.organization_id):
        for enrollment_id in (setup.enrollment_id, uuid4()):
            with pytest.raises(AppointmentAccessDeniedError):
                prepare_booking(
                    clinic_id=setup.clinic_id,
                    enrollment_id=enrollment_id,
                )

    with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            ["not-a-uuid"],
        )
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
            [str(setup.actor_id)],
        )
        with pytest.raises(AppointmentAccessDeniedError):
            view_appointment_for_transition(appointment_id=appointment.pk)

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
        raise ForcedAgendaAuditFailureError

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        monkeypatch.setattr(agenda_queries, "record_phase1_event", append_then_fail)
        with pytest.raises(ForcedAgendaAuditFailureError):
            view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date="2035-06-04",
                page=1,
            )
        monkeypatch.setattr(agenda_queries, "record_phase1_event", original_append)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type IN ("
                "'scheduling.booking.viewed', "
                "'scheduling.appointment.viewed', "
                "'scheduling.agenda.viewed')"
            )
            final_view_count = cursor.fetchone()
        verification = verify_chain(setup.organization_id)

    assert final_view_count == (3,)
    assert verification.valid is True

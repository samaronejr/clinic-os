from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.services import record_phase1_event, verify_chain
from apps.scheduling import appointment_creation
from apps.scheduling.models import Appointment
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


class ForcedAppointmentAuditFailureError(Exception):
    pass


def test_failed_audit_rolls_back_booking_and_leaves_key_reusable(
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
        raise ForcedAppointmentAuditFailureError

    key = uuid4()
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        monkeypatch.setattr(
            appointment_creation,
            "record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedAppointmentAuditFailureError):
            create_synthetic_appointment(setup, idempotency_key=key)
        assert Appointment.objects.count() == 0
        monkeypatch.setattr(
            appointment_creation,
            "record_phase1_event",
            original_append,
        )
        accepted = create_synthetic_appointment(setup, idempotency_key=key)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_id FROM "
                "clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            appointment_events = cursor.fetchall()
        verification = verify_chain(setup.organization_id)

    assert accepted.idempotency_key == key
    assert appointment_events == [("scheduling.appointment.created", str(accepted.pk))]
    assert verification.valid is True
    assert verification.row_count == 3

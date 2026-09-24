from __future__ import annotations

from datetime import UTC, datetime
from inspect import Parameter, signature
from types import FunctionType
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling import services
from apps.scheduling.models import Appointment
from apps.scheduling.services import AppointmentLocalRange
from apps.tenancy.db import tenant_context
from django.db import connection

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_receptionist_reschedules_in_place_with_exact_public_contract_and_audit(
    rbac_graph: RbacGraph,
) -> None:
    entrypoint = getattr(services, "reschedule_appointment", None)
    assert isinstance(entrypoint, FunctionType)
    parameters = signature(entrypoint).parameters
    assert list(parameters) == ["appointment_id", "local_range"]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())

    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        original = create_synthetic_appointment(setup, idempotency_key=key)
        fingerprint = bytes(original.create_fingerprint)
        transitioned = entrypoint(
            appointment_id=original.pk,
            local_range=AppointmentLocalRange(
                start_local="2035-06-02T10:00",
                end_local="2035-06-02T11:00",
            ),
        )
        assert isinstance(transitioned, Appointment)
        appointment_count = Appointment.objects.count()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.rescheduled'"
            )
            audit_rows = cursor.fetchall()

    assert transitioned.pk == original.pk
    assert transitioned.idempotency_key == key
    assert bytes(transitioned.create_fingerprint) == fingerprint
    assert transitioned.start_at == datetime(2035, 6, 2, 13, 0, tzinfo=UTC)
    assert transitioned.end_at == datetime(2035, 6, 2, 14, 0, tzinfo=UTC)
    assert appointment_count == 1
    assert audit_rows == [
        (
            "scheduling.appointment.rescheduled",
            "scheduling.appointment",
            str(original.pk),
            2,
            str(setup.clinic_id),
            "rescheduled",
        )
    ]

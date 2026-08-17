from __future__ import annotations

from inspect import Parameter, signature
from types import FunctionType
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling import services
from apps.scheduling.models import Appointment
from apps.tenancy.db import tenant_context
from django.db import connection
from django.utils import timezone

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_cancellation_preserves_history_frees_slots_and_appends_exactly_once(
    rbac_graph: RbacGraph,
) -> None:
    entrypoint = getattr(services, "cancel_appointment", None)
    assert isinstance(entrypoint, FunctionType)
    parameters = signature(entrypoint).parameters
    assert list(parameters) == ["appointment_id", "reason"]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())

    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        original = create_synthetic_appointment(setup)
        before = timezone.now()
        cancelled = entrypoint(
            appointment_id=original.pk,
            reason=Appointment.CancellationReason.CLINIC_REQUEST,
        )
        after = timezone.now()
        assert isinstance(cancelled, Appointment)
        replacement = create_synthetic_appointment(setup, idempotency_key=uuid4())
        appointment_count = Appointment.objects.count()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT event_type, affected_record_type, affected_record_id, "
                "(SELECT count(*) FROM jsonb_object_keys(payload)), "
                "payload ->> 'clinic_id', payload ->> 'object_verb' "
                "FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.cancelled'"
            )
            audit_rows = cursor.fetchall()

    assert cancelled.pk == original.pk
    assert cancelled.status == Appointment.Status.CANCELLED
    assert (
        cancelled.cancellation_reason == Appointment.CancellationReason.CLINIC_REQUEST
    )
    assert cancelled.cancelled_at is not None
    assert before <= cancelled.cancelled_at <= after
    assert replacement.pk != original.pk
    assert replacement.start_at == original.start_at
    assert replacement.end_at == original.end_at
    assert appointment_count == 2
    assert audit_rows == [
        (
            "scheduling.appointment.cancelled",
            "scheduling.appointment",
            str(original.pk),
            2,
            str(setup.clinic_id),
            "cancelled",
        )
    ]

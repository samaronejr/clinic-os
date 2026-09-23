from __future__ import annotations

import importlib
from inspect import Parameter, signature
from typing import TYPE_CHECKING

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.models import Appointment
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


def test_transition_view_uses_uuid_fallback_for_revoked_historical_physician(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    view_appointment = getattr(services, "view_appointment_for_transition", None)
    assert callable(view_appointment)
    parameters = signature(view_appointment).parameters
    assert list(parameters) == ["appointment_id"]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())

    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
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
        result = view_appointment(appointment_id=appointment.pk)

    assert result.appointment_id == appointment.pk
    assert result.clinic_id == setup.clinic_id
    assert result.patient_display_name == "Synthetic Booking Persona"
    assert result.practitioner_id == setup.practitioner_id
    assert result.practitioner_display_identifier == str(setup.practitioner_id)
    assert result.start_local == "2035-06-02T09:00"
    assert result.end_local == "2035-06-02T10:00"
    assert result.status == Appointment.Status.SCHEDULED
    assert result.cancellation_reason is None
    assert not hasattr(result, "birth_date")

from __future__ import annotations

import importlib
from inspect import Parameter, signature
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User
from apps.scheduling.services import (
    AppointmentAvailabilityError,
    AppointmentLocalRange,
    create_appointment,
    create_availability,
    retire_availability,
)
from apps.tenancy.db import tenant_context

from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_appointment_setup

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_booking_preparation_exposes_active_windows_without_generating_slots(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    prepare_booking = getattr(services, "prepare_booking", None)
    assert callable(prepare_booking)
    parameters = signature(prepare_booking).parameters
    assert list(parameters) == ["clinic_id", "enrollment_id"]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())

    setup = seed_appointment_setup(rbac_graph)
    physician_username = User.objects.get(pk=setup.practitioner_id).username
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        retired = create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
            start_local="2035-06-03T08:00",
            end_local="2035-06-03T12:00",
            idempotency_key=uuid4(),
        )
        retire_availability(
            clinic_id=setup.clinic_id,
            availability_id=retired.pk,
        )
        preparation = prepare_booking(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
        )
        start_boundary = create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            local_range=AppointmentLocalRange(
                "2035-06-02T08:00",
                "2035-06-02T08:30",
            ),
            idempotency_key=uuid4(),
        )
        inside = create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            local_range=AppointmentLocalRange(
                "2035-06-02T09:00",
                "2035-06-02T10:00",
            ),
            idempotency_key=uuid4(),
        )
        end_boundary = create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            local_range=AppointmentLocalRange(
                "2035-06-02T11:30",
                "2035-06-02T12:00",
            ),
            idempotency_key=uuid4(),
        )
        with pytest.raises(AppointmentAvailabilityError):
            create_appointment(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
                practitioner_id=setup.practitioner_id,
                local_range=AppointmentLocalRange(
                    "2035-06-02T12:00",
                    "2035-06-02T12:30",
                ),
                idempotency_key=uuid4(),
            )

    assert preparation.enrollment_id == setup.enrollment_id
    assert preparation.patient_display_name == "Synthetic Booking Persona"
    assert not hasattr(preparation, "birth_date")
    assert not hasattr(preparation, "slots")
    assert not hasattr(preparation, "fixed_duration")
    assert len(preparation.practitioners) == 1
    practitioner = preparation.practitioners[0]
    assert practitioner.practitioner_id == setup.practitioner_id
    assert practitioner.display_identifier == physician_username
    assert [window.availability_id for window in practitioner.windows] != [retired.pk]
    assert [
        (window.start_local, window.end_local) for window in practitioner.windows
    ] == [("2035-06-02T08:00", "2035-06-02T12:00")]
    assert start_boundary.start_at < inside.start_at < end_boundary.start_at

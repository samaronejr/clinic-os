from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.appointment_creation import (
    ServiceBooking,
    create_service_appointment,
)
from apps.scheduling.models import (
    Appointment,
    AppointmentResource,
    AvailabilityBlock,
    Resource,
    ServiceType,
)
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_services import (
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
)
from apps.scheduling.services import AppointmentLocalRange
from apps.tenancy.db import tenant_context
from django.db import connection, connections

from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.resource_test_support import independent_setup

if TYPE_CHECKING:
    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("resource_clock"),
]


def _catalog(setup: AppointmentSetup) -> tuple[Resource, Resource, ServiceType]:
    room = create_resource(
        clinic_id=setup.clinic_id,
        content=ResourceInput(name="Sintetico room", kind="room"),
    )
    equipment = create_resource(
        clinic_id=setup.clinic_id,
        content=ResourceInput(name="Sintetico ultrasound", kind="equipment"),
    )
    service = create_service_type(
        clinic_id=setup.clinic_id,
        content=ServiceInput(
            name="Sintetico ultrasound service",
            duration_min=30,
            buffer_before=10,
            buffer_after=10,
            required_resource_kinds=("room", "equipment"),
        ),
    )
    for resource in (room, equipment):
        template = create_template(
            clinic_id=setup.clinic_id,
            content=TemplateInput(
                resource_id=resource.pk,
                weekdays=(5,),
                start_local=time(8),
                end_local=time(12),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 2),
            ),
        )
        first = generate_availability(
            clinic_id=setup.clinic_id,
            template_id=template.pk,
            start_date=date(2035, 6, 2),
            end_date=date(2035, 6, 2),
        )
        replay = generate_availability(
            clinic_id=setup.clinic_id,
            template_id=template.pk,
            start_date=date(2035, 6, 2),
            end_date=date(2035, 6, 2),
        )
        assert first == replay
        assert len(first) == 1
    return room, equipment, service


def test_service_reserves_room_and_ultrasound_with_buffers(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, equipment, service = _catalog(setup)
        appointment = create_service_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=setup.enrollment_id,
            practitioner_id=setup.practitioner_id,
            booking=ServiceBooking(
                AppointmentLocalRange("2035-06-02T09:00", "2035-06-02T09:30"),
                service.pk,
                (room.pk, equipment.pk),
            ),
            idempotency_key=uuid4(),
        )
        assert Appointment.objects.count() == 1
        assert AvailabilityBlock.objects.filter(resource__isnull=False).count() == 2
        reservations = list(AppointmentResource.objects.filter(appointment=appointment))
        assert {row.resource_id for row in reservations} == {room.pk, equipment.pk}
        assert all(row.occupied and row.unit == 1 for row in reservations)
        assert all(
            row.start_at.minute == 50 and row.end_at.minute == 40
            for row in reservations
        )


@pytest.mark.parametrize("workers", [2, 20])
def test_same_room_barrier_has_one_success(
    rbac_graph: RbacGraph,
    workers: int,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room, _, _ = _catalog(setup)
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico shared room",
                duration_min=30,
                required_resource_kinds=("room",),
            ),
        )
    setups = [setup, *(independent_setup(setup, index) for index in range(1, workers))]
    barrier = Barrier(workers, timeout=30)

    def book(target: AppointmentSetup) -> str:
        connections.close_all()
        try:
            with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout='30s'")
                barrier.wait()
                try:
                    create_service_appointment(
                        clinic_id=target.clinic_id,
                        enrollment_id=target.enrollment_id,
                        practitioner_id=target.practitioner_id,
                        booking=ServiceBooking(
                            AppointmentLocalRange(
                                "2035-06-02T09:00", "2035-06-02T09:30"
                            ),
                            service.pk,
                            (room.pk,),
                        ),
                        idempotency_key=uuid4(),
                    )
                except SchedulingRuleError as error:
                    return error.code
                return "booked"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(book, setups, timeout=45))
    assert results.count("booked") == 1
    assert results.count("resource_conflict") == workers - 1
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Appointment.objects.count() == 1
        assert AppointmentResource.objects.count() == 1

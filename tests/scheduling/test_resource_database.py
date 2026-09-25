from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from threading import Barrier
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.appointment_persistence import _constraint_name
from apps.scheduling.models import Appointment, AppointmentResource
from apps.scheduling.resource_services import (
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
)
from apps.scheduling.timezones import parse_local_minute
from apps.tenancy.db import tenant_context
from django.db import IntegrityError, connection, connections, transaction

from patient_service_support import runtime_role
from scheduling.appointment_service_support import seed_appointment_setup
from scheduling.resource_test_support import independent_setup

if TYPE_CHECKING:
    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize("capacity", [1, 2])
def test_direct_sql_bookings_cannot_exceed_room_capacity(
    rbac_graph: RbacGraph, capacity: int
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    targets = [
        setup,
        *(independent_setup(setup, index) for index in range(1, capacity + 1)),
    ]
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        room = create_resource(
            clinic_id=setup.clinic_id,
            content=ResourceInput(
                name="Sintetico pool", kind="room", capacity=capacity
            ),
        )
        service = create_service_type(
            clinic_id=setup.clinic_id,
            content=ServiceInput(
                name="Sintetico pool service",
                duration_min=30,
                required_resource_kinds=("room",),
            ),
        )
        template = create_template(
            clinic_id=setup.clinic_id,
            content=TemplateInput(
                resource_id=room.pk,
                weekdays=(5,),
                start_local=time(8),
                end_local=time(12),
                valid_from=date(2035, 6, 2),
                valid_to=date(2035, 6, 2),
            ),
        )
        generate_availability(
            clinic_id=setup.clinic_id,
            template_id=template.pk,
            start_date=date(2035, 6, 2),
            end_date=date(2035, 6, 2),
        )
    barrier = Barrier(capacity + 1, timeout=20)

    def raw_book(target: AppointmentSetup) -> str:
        connections.close_all()
        try:
            with (
                runtime_role(),
                tenant_context(target.actor_id, target.organization_id),
            ):
                with connection.cursor() as cursor:
                    cursor.execute("SET LOCAL statement_timeout='30s'")
                barrier.wait()
                try:
                    with transaction.atomic():
                        Appointment.objects.create(
                            organization_id=target.organization_id,
                            clinic_id=target.clinic_id,
                            patient_id=target.patient_id,
                            practitioner_id=target.practitioner_id,
                            service_type=service,
                            resource_ids=[room.pk],
                            start_at=parse_local_minute(
                                "2035-06-02T09:00", "America/Sao_Paulo"
                            ),
                            end_at=parse_local_minute(
                                "2035-06-02T09:30", "America/Sao_Paulo"
                            ),
                            idempotency_key=uuid4(),
                            create_fingerprint=b"s" * 32,
                        )
                except IntegrityError as error:
                    return str(_constraint_name(error))
                return "booked"
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=capacity + 1) as executor:
        outcomes = list(executor.map(raw_book, targets, timeout=45))
    assert outcomes.count("booked") == capacity
    assert outcomes.count("scheduling_resource_conflict") == 1
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        assert Appointment.objects.count() == capacity
        assert set(AppointmentResource.objects.values_list("unit", flat=True)) == set(
            range(1, capacity + 1)
        )
        assert (
            AppointmentResource.objects.filter(resource=room, occupied=True).count()
            == capacity
        )

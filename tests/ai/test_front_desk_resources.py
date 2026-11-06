"""A real resource-bearing acceptance acquires resource gates before contact."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import time
from threading import Event
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.locks import acquire_advisory_locks, resource_lock_keys
from apps.scheduling.resource_services import (
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_resource,
    create_service_type,
    create_template,
    generate_availability,
)
from apps.scheduling.services import (
    AppointmentLocalRange,
    ServiceBooking,
    cancel_appointment,
    create_service_appointment,
)
from django.db import connection, connections

from ai.test_front_desk import (
    Desk,
    appointment_start,
    desk_world,
    say,
    thread_of,
    turns,
    verify,
)
from ehr.test_autosave import as_actor

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from uuid import UUID

    from pytest_django.fixtures import SettingsWrapper

    from rbac_fixtures import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def resource_appointment(desk: Desk) -> tuple[Desk, UUID]:
    day = appointment_start(desk).astimezone(desk.zone).date()
    with as_actor(desk.graph, desk.manager):
        cancel_appointment(appointment_id=desk.appointment, reason="patient_request")
        room = create_resource(
            clinic_id=desk.graph.clinic_a,
            content=ResourceInput(name="Synthetic front desk room", kind="room"),
        )
        service = create_service_type(
            clinic_id=desk.graph.clinic_a,
            content=ServiceInput(
                name="Synthetic front desk service",
                duration_min=30,
                required_resource_kinds=("room",),
            ),
        )
        template = create_template(
            clinic_id=desk.graph.clinic_a,
            content=TemplateInput(
                resource_id=room.pk,
                weekdays=(day.weekday(),),
                start_local=time(8),
                end_local=time(12),
                valid_from=day,
                valid_to=day,
            ),
        )
        generate_availability(
            clinic_id=desk.graph.clinic_a,
            template_id=template.pk,
            start_date=day,
            end_date=day,
        )
        appointment = create_service_appointment(
            clinic_id=desk.graph.clinic_a,
            enrollment_id=desk.enrollment,
            practitioner_id=desk.graph.physician,
            booking=ServiceBooking(
                AppointmentLocalRange(f"{day}T10:00", f"{day}T10:30"),
                service.pk,
                (room.pk,),
            ),
            idempotency_key=uuid4(),
        )
    return replace(desk, appointment=appointment.pk), room.pk


def test_resource_acceptance_locks_resource_before_contact_mutation_key(
    rbac_graph: RbacGraph,
    settings: SettingsWrapper,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    desk, resource = resource_appointment(desk_world(rbac_graph, settings, monkeypatch))
    verify(desk)
    say(desk, "Quero remarcar")
    offered = thread_of(desk).offered_start
    assert offered is not None
    locked, release, attempted, contacted = Event(), Event(), Event(), Event()
    acquired: list[str] = []
    resource_key = resource_lock_keys((resource,))[0]

    def hold_resource() -> None:
        try:
            with as_actor(rbac_graph, desk.manager):
                acquire_advisory_locks((resource_key,))
                locked.set()
                assert release.wait(timeout=30)
        finally:
            connections.close_all()

    def observe(
        execute: Callable[..., object],
        sql: str,
        params: Sequence[object] | None,
        many: bool,
        context: dict[str, object],
    ) -> object:
        resource_gate = (
            "pg_catalog.pg_advisory_xact_lock(" in sql
            and params is not None
            and params[0] == resource_key
        )
        if resource_gate:
            attempted.set()
        result = execute(sql, params, many, context)
        if resource_gate:
            acquired.append("resource")
        if "clinic_app.touch_patient_session(" in sql:
            acquired.append("contact")
            contacted.set()
        return result

    def accept() -> None:
        try:
            with connection.execute_wrapper(observe):
                say(desk, "SIM")
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as pool:
        holder = pool.submit(hold_resource)
        assert locked.wait(timeout=30)
        booking = pool.submit(accept)
        try:
            assert attempted.wait(timeout=30)
            assert not contacted.is_set()
        finally:
            release.set()
        holder.result(timeout=40)
        booking.result(timeout=40)
    assert acquired.index("resource") < acquired.index("contact")
    assert appointment_start(desk) == offered
    assert turns(desk)[-1].outcome == "booked"

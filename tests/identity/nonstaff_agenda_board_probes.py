"""Observe the agenda board's permission roots on a real service booking."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.scheduling import agenda_board, agenda_board_views
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.resource_definitions import ServiceInput, create_service_type
from apps.scheduling.services import (
    ServiceBooking,
    create_availability,
    create_service_appointment,
)
from apps.tenancy.db import tenant_context

from identity.nonstaff_differential import DifferentialProbe
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from collections.abc import Callable

    from identity.nonstaff_subjects import NonstaffSubjects
    from rbac_fixtures import RbacGraph


def agenda_board_probes(
    data: NonstaffSubjects, graph: RbacGraph
) -> list[DifferentialProbe]:
    """Seed with staff authority, then observe refusals as the census actor."""
    day = "2035-06-13"
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.physician,
            start_local=f"{day}T08:00",
            end_local=f"{day}T12:00",
            idempotency_key=uuid4(),
        )
        service = create_service_type(
            clinic_id=graph.clinic_a,
            content=ServiceInput(name="Sintetico board census", duration_min=30),
        )
        appointment = create_service_appointment(
            clinic_id=graph.clinic_a,
            enrollment_id=data.op.enrollment,
            practitioner_id=graph.physician,
            booking=ServiceBooking(
                AppointmentLocalRange(f"{day}T08:00", f"{day}T08:30"),
                service.pk,
                (),
            ),
            idempotency_key=uuid4(),
        )
    calls: dict[str, Callable[[], object]] = {
        "apps.scheduling.agenda_board.view_agenda_board": (
            lambda: agenda_board.view_agenda_board(
                clinic_id=graph.clinic_a, view="week", date=day
            )
        ),
        "apps.scheduling.agenda_board_views.agenda_board_action_view": (
            lambda: inspect.unwrap(agenda_board_views.agenda_board_action_view)(
                data.request(
                    {
                        "intent": "arrive",
                        "view": "day",
                        "day": day,
                        "appointment_id": str(appointment.pk),
                        "expected_revision": str(appointment.revision),
                        "command_id": str(uuid4()),
                    }
                ),
                graph.clinic_a,
            )
        ),
    }
    return [
        DifferentialProbe(symbol, invoke, expected=False)
        for symbol, invoke in calls.items()
    ]

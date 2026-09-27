"""Scheduling adapters for the behavioural classification census (todo 21).

booking_selection, _template and require_open_window run after their callers'
permission boundary. They read the clinic row and the tenant setting through
RLS, so they are observed boundaries rather than nonstaff exemptions: the
observer must see that evidence and the decisions are compared exactly.
Every probe has a granted and a refused control with exact decisions.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, time
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import Clinic
from apps.scheduling import resource_booking, resource_services
from apps.scheduling.models import AvailabilityTemplate, ServiceType
from apps.scheduling.resource_errors import SchedulingRuleError
from apps.scheduling.resource_services import (
    ClosureInput,
    ResourceInput,
    ServiceInput,
    TemplateInput,
    create_closure,
    create_resource,
    create_service_type,
    create_template,
)
from apps.tenancy.db import tenant_context

from identity.nonstaff_differential import DifferentialProbe
from identity.permission_support import owner_context
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from uuid import UUID

    from identity.nonstaff_subjects import NonstaffSubjects

DAY = date(2035, 9, 4)
OPEN = (datetime(2035, 9, 4, 12, tzinfo=UTC), datetime(2035, 9, 4, 13, tzinfo=UTC))
CLOSED = (datetime(2035, 9, 5, 12, tzinfo=UTC), datetime(2035, 9, 5, 13, tzinfo=UTC))


def _open(
    clinic: UUID, practitioner: UUID, room: UUID, window: tuple[datetime, datetime]
) -> bool:
    try:
        resource_services.require_open_window(
            clinic_id=clinic,
            practitioner_id=practitioner,
            resource_ids=(room,),
            start_at=window[0],
            end_at=window[1],
        )
    except SchedulingRuleError:
        return False
    return True


def _selected(result: object, service: UUID, room: UUID) -> bool:
    assert isinstance(result, tuple)
    chosen, resources = result
    return (
        isinstance(chosen, ServiceType)
        and chosen.pk == service
        and [row.pk for row in resources] == [room]
    )


def scheduling_probes(d: NonstaffSubjects) -> list[DifferentialProbe]:
    graph = d.legacy.graph
    clinic_id = d.legacy.clinic
    # Seeded by a permitted booker before any observed call; the observed
    # helpers only receive the resolved clinic row and opaque selectors.
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        room = create_resource(
            clinic_id=clinic_id,
            content=ResourceInput(name="Sintetico census selection", kind="room"),
        )
        service = create_service_type(
            clinic_id=clinic_id,
            content=ServiceInput(
                name="Sintetico census selection",
                duration_min=30,
                required_resource_kinds=("room",),
            ),
        )
        template = create_template(
            clinic_id=clinic_id,
            content=TemplateInput(
                resource_id=room.pk,
                weekdays=(DAY.weekday(),),
                start_local=time(8),
                end_local=time(12),
                valid_from=DAY,
                valid_to=DAY,
            ),
        )
        create_closure(
            clinic_id=clinic_id,
            content=ClosureInput(
                start_local="2035-09-05T08:00",
                end_local="2035-09-05T18:00",
                reason="holiday",
            ),
        )
    with owner_context(graph.organization_a):
        clinic = Clinic.objects.get(pk=clinic_id)
    practitioner = graph.physician
    return [
        DifferentialProbe(
            "apps.scheduling.resource_booking.booking_selection",
            lambda: resource_booking.booking_selection(
                clinic=clinic, service_type_id=service.pk, resource_ids=(room.pk,)
            ),
            decision=lambda result: _selected(result, service.pk, room.pk),
        ),
        DifferentialProbe(
            "apps.scheduling.resource_booking.booking_selection",
            lambda: resource_booking.booking_selection(
                clinic=clinic, service_type_id=uuid4(), resource_ids=(room.pk,)
            ),
            expected=False,
        ),
        DifferentialProbe(
            "apps.scheduling.resource_services._template",
            lambda: resource_services._template(clinic, template.pk),
            decision=lambda result: (
                isinstance(result, AvailabilityTemplate) and result.pk == template.pk
            ),
        ),
        DifferentialProbe(
            "apps.scheduling.resource_services._template",
            lambda: resource_services._template(clinic, uuid4()),
            expected=False,
        ),
        DifferentialProbe(
            "apps.scheduling.resource_services.require_open_window",
            lambda: _open(clinic_id, practitioner, room.pk, OPEN),
        ),
        DifferentialProbe(
            "apps.scheduling.resource_services.require_open_window",
            lambda: _open(clinic_id, practitioner, room.pk, CLOSED),
            expected=False,
        ),
    ]

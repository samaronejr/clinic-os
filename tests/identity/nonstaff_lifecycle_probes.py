"""Observe lifecycle delegations on real appointments and a stored series."""

from __future__ import annotations

import inspect
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.scheduling import appointment_lifecycle, appointment_series, lifecycle_views
from apps.scheduling.appointment_values import AppointmentLocalRange
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context

from identity.nonstaff_differential import DifferentialProbe
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from identity.nonstaff_subjects import NonstaffSubjects
    from rbac_fixtures import RbacGraph

SYMBOLS = frozenset(
    {
        "apps.scheduling.appointment_lifecycle.authorize_transition",
        "apps.scheduling.appointment_lifecycle._transition",
        "apps.scheduling.appointment_series._require_edit_authority",
        "apps.scheduling.appointment_series.create_series",
        "apps.scheduling.appointment_series.edit_series",
        "apps.scheduling.lifecycle_views.appointment_lifecycle_view",
    }
)


def lifecycle_probes(
    data: NonstaffSubjects, graph: RbacGraph
) -> list[DifferentialProbe]:
    """Seed as staff, then observe the same valid subjects as the census actor."""
    booking = appointment_series.SeriesBooking(
        AppointmentLocalRange("2035-06-04T08:00", "2035-06-04T08:30"),
        "weekly",
        count=1,
    )
    key = uuid4()
    with runtime_role(), tenant_context(graph.shared_user, graph.organization_a):
        create_availability(
            clinic_id=graph.clinic_a,
            practitioner_id=graph.physician,
            start_local="2035-06-04T08:00",
            end_local="2035-06-04T10:00",
            idempotency_key=uuid4(),
        )
        series = appointment_series.create_series(
            clinic_id=graph.clinic_a,
            enrollment_id=data.op.enrollment,
            practitioner_id=graph.physician,
            booking=booking,
            idempotency_key=key,
        )
    appointment = data.appointment
    calls = {
        "apps.scheduling.appointment_lifecycle.authorize_transition": (
            lambda: appointment_lifecycle.authorize_transition(
                "arrive", appointment, appointment.status
            )
        ),
        "apps.scheduling.appointment_lifecycle._transition": (
            lambda: appointment_lifecycle._transition(
                "arrive",
                clinic_id=graph.clinic_a,
                appointment_id=appointment.pk,
                expected_revision=appointment.revision,
                command_id=uuid4(),
            )
        ),
        "apps.scheduling.appointment_series._require_edit_authority": (
            lambda: appointment_series._require_edit_authority(series)
        ),
        "apps.scheduling.appointment_series.create_series": (
            lambda: appointment_series.create_series(
                clinic_id=graph.clinic_a,
                enrollment_id=data.op.enrollment,
                practitioner_id=graph.physician,
                booking=booking,
                idempotency_key=key,
            )
        ),
        "apps.scheduling.appointment_series.edit_series": (
            lambda: appointment_series.edit_series(
                clinic_id=graph.clinic_a,
                series_id=series.pk,
                edit=appointment_series.SeriesEdit("cancelled", 1, "this"),
                command_id=uuid4(),
            )
        ),
        "apps.scheduling.lifecycle_views.appointment_lifecycle_view": (
            lambda: inspect.unwrap(lifecycle_views.appointment_lifecycle_view)(
                data.request(
                    {
                        "appointment_id": str(appointment.pk),
                        "action": "arrive",
                        "expected_revision": str(appointment.revision),
                        "command_id": str(uuid4()),
                    }
                ),
                graph.clinic_a,
            )
        ),
    }
    assert set(calls) == SYMBOLS
    return [
        DifferentialProbe(symbol, invoke, expected=False)
        for symbol, invoke in calls.items()
    ]

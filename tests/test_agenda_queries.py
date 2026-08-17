from __future__ import annotations

import importlib
from datetime import UTC, datetime, timedelta
from inspect import Parameter, signature
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import Clinic
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentLocalRange,
    cancel_appointment,
    create_appointment,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from appointment_service_support import seed_appointment_setup
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def _set_clinic_timezone(clinic_id: UUID, organization_id: UUID, zone: str) -> None:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(organization_id)],
        )
        Clinic.objects.filter(pk=clinic_id).update(timezone=zone)


def test_day_week_agenda_uses_civil_bounds_and_stable_pagination(
    rbac_graph: RbacGraph,
) -> None:
    services = importlib.import_module("apps.scheduling.services")
    view_agenda = getattr(services, "view_agenda", None)
    assert callable(view_agenda)
    parameters = signature(view_agenda).parameters
    assert list(parameters) == ["clinic_id", "view", "date", "page"]
    assert all(value.kind is Parameter.KEYWORD_ONLY for value in parameters.values())
    setup = seed_appointment_setup(rbac_graph)
    appointments = []
    first_minute = datetime(2035, 6, 2, 8, 0, tzinfo=UTC)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        for index in range(27):
            start = first_minute + timedelta(minutes=index)
            end = start + timedelta(minutes=1)
            appointments.append(
                create_appointment(
                    clinic_id=setup.clinic_id,
                    enrollment_id=setup.enrollment_id,
                    practitioner_id=setup.practitioner_id,
                    local_range=AppointmentLocalRange(
                        start.strftime("%Y-%m-%dT%H:%M"),
                        end.strftime("%Y-%m-%dT%H:%M"),
                    ),
                    idempotency_key=uuid4(),
                )
            )
        cancel_appointment(
            appointment_id=appointments[3].pk,
            reason=Appointment.CancellationReason.OTHER,
        )
        day_first = view_agenda(
            clinic_id=setup.clinic_id,
            view="day",
            date="2035-06-02",
            page=1,
        )
        day_second = view_agenda(
            clinic_id=setup.clinic_id,
            view="day",
            date="2035-06-02",
            page=2,
        )
        week = view_agenda(
            clinic_id=setup.clinic_id,
            view="week",
            date="2035-06-02",
            page=1,
        )

    assert day_first.total == 27
    assert day_first.page_count == 2
    assert [item.appointment_id for item in day_first.items] == [
        item.pk for item in appointments[:25]
    ]
    assert [item.appointment_id for item in day_second.items] == [
        item.pk for item in appointments[25:]
    ]
    assert day_first.items[3].status == Appointment.Status.CANCELLED
    assert not hasattr(day_first.items[0], "birth_date")
    assert week.total == 27
    assert day_first.start_at == datetime(2035, 6, 2, 3, 0, tzinfo=UTC)
    assert day_first.end_at == datetime(2035, 6, 3, 3, 0, tzinfo=UTC)
    assert week.start_at == datetime(2035, 5, 28, 3, 0, tzinfo=UTC)
    assert week.end_at == datetime(2035, 6, 4, 3, 0, tzinfo=UTC)

    vectors = (
        (
            "America/Sao_Paulo",
            "2018-02-18",
            datetime(2018, 2, 18, 3, 0, tzinfo=UTC),
            datetime(2018, 2, 19, 3, 0, tzinfo=UTC),
        ),
        (
            "America/Sao_Paulo",
            "2018-11-04",
            datetime(2018, 11, 4, 3, 0, tzinfo=UTC),
            datetime(2018, 11, 5, 2, 0, tzinfo=UTC),
        ),
        (
            "Pacific/Apia",
            "2011-12-30",
            datetime(2011, 12, 30, 10, 0, tzinfo=UTC),
            datetime(2011, 12, 30, 10, 0, tzinfo=UTC),
        ),
    )
    for zone, civil_date, expected_start, expected_end in vectors:
        _set_clinic_timezone(setup.clinic_id, setup.organization_id, zone)
        with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
            result = view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date=civil_date,
                page=1,
            )
        assert result.start_at == expected_start
        assert result.end_at == expected_end
        assert result.items == ()

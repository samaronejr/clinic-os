from __future__ import annotations

from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.intake.services import create_patient
from apps.scheduling.services import (
    AgendaInputError,
    AppointmentAccessDeniedError,
    AppointmentLocalRange,
    AvailabilityAccessDeniedError,
    create_appointment,
    create_availability,
    prepare_booking,
    view_agenda,
    view_appointment_for_transition,
)
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from apps.scheduling.models import Appointment

    from conftest import RbacGraph
    from scheduling.appointment_service_support import AppointmentSetup

pytestmark = pytest.mark.django_db(transaction=True)


def _seed_two_agendas(
    rbac_graph: RbacGraph,
) -> tuple[AppointmentSetup, Appointment, Appointment]:
    setup = seed_appointment_setup(rbac_graph)
    second_physician = User.objects.create(
        username=f"todo12-synthetic-physician-{uuid4().hex}",
        password=make_password(None),
    )
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        UserClinicRole.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user=second_physician,
            role=UserClinicRole.Role.PHYSICIAN,
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        first = create_synthetic_appointment(setup)
        second_registration = create_patient(
            clinic_id=setup.clinic_id,
            full_name="Synthetic Agenda Persona Two",
            birth_date=date(2000, 6, 2),
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=second_physician.pk,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
        second = create_appointment(
            clinic_id=setup.clinic_id,
            enrollment_id=second_registration.enrollment.pk,
            practitioner_id=second_physician.pk,
            local_range=AppointmentLocalRange(
                "2035-06-02T10:00",
                "2035-06-02T11:00",
            ),
            idempotency_key=uuid4(),
        )
    return setup, first, second


def _agenda_ids(
    setup: AppointmentSetup,
    actor_id: UUID,
    organization_id: UUID,
) -> list[UUID]:
    with runtime_role(), tenant_context(actor_id, organization_id):
        return [
            item.appointment_id
            for item in view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date="2035-06-02",
                page=1,
            ).items
        ]


def _assert_role_union(
    setup: AppointmentSetup,
    first: Appointment,
    second: Appointment,
) -> list[AppointmentAccessDeniedError]:
    manager_ids = _agenda_ids(setup, setup.actor_id, setup.organization_id)
    physician_ids = _agenda_ids(
        setup,
        setup.practitioner_id,
        setup.organization_id,
    )

    appointment_probe_errors: list[AppointmentAccessDeniedError] = []
    booking_probe_errors: list[AppointmentAccessDeniedError] = []
    with runtime_role(), tenant_context(setup.practitioner_id, setup.organization_id):
        for appointment_id in (first.pk, uuid4()):
            with pytest.raises(AppointmentAccessDeniedError) as appointment_error:
                view_appointment_for_transition(appointment_id=appointment_id)
            appointment_probe_errors.append(appointment_error.value)
        for enrollment_id in (setup.enrollment_id, uuid4()):
            with pytest.raises(AppointmentAccessDeniedError) as booking_error:
                prepare_booking(
                    clinic_id=setup.clinic_id,
                    enrollment_id=enrollment_id,
                )
            booking_probe_errors.append(booking_error.value)

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        UserClinicRole.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user_id=setup.practitioner_id,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
    multi_role_ids = _agenda_ids(
        setup,
        setup.practitioner_id,
        setup.organization_id,
    )

    assert manager_ids == [first.pk, second.pk]
    assert physician_ids == [first.pk]
    assert multi_role_ids == manager_ids
    assert {error.args for error in appointment_probe_errors} == {
        ("appointment access denied",)
    }
    assert {error.args for error in booking_probe_errors} == {
        ("appointment access denied",)
    }
    return appointment_probe_errors


def _assert_authorized_input_and_scope_denials(
    rbac_graph: RbacGraph,
    setup: AppointmentSetup,
) -> list[AvailabilityAccessDeniedError]:
    agenda_denials: list[AvailabilityAccessDeniedError] = []

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        for clinic_id in (rbac_graph.clinic_b, rbac_graph.clinic_c):
            with pytest.raises(AvailabilityAccessDeniedError) as agenda_error:
                view_agenda(
                    clinic_id=clinic_id,
                    view="day",
                    date="2035-06-02",
                    page=1,
                )
            agenda_denials.append(agenda_error.value)
        for view, civil_date, page in (
            ("month", "2035-06-02", 1),
            ("day", "20350602", 1),
            ("week", "2035-06-02", 0),
        ):
            with pytest.raises(AgendaInputError):
                view_agenda(
                    clinic_id=setup.clinic_id,
                    view=view,
                    date=civil_date,
                    page=page,
                )

    with runtime_role(), tenant_context(rbac_graph.clinic_admin, setup.organization_id):
        with pytest.raises(AvailabilityAccessDeniedError) as unassigned_error:
            view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date="2035-06-02",
                page=1,
            )
        agenda_denials.append(unassigned_error.value)
    with runtime_role(), tenant_context(setup.actor_id, rbac_graph.organization_b):
        with pytest.raises(AvailabilityAccessDeniedError) as foreign_error:
            view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date="2035-06-02",
                page=1,
            )
        agenda_denials.append(foreign_error.value)

    User.objects.filter(pk=setup.actor_id).update(is_active=False)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(AvailabilityAccessDeniedError) as inactive_error:
            view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date="2035-06-02",
                page=1,
            )
        agenda_denials.append(inactive_error.value)
    User.objects.filter(pk=setup.actor_id).update(is_active=True)
    return agenda_denials


def _assert_raw_context_denials(
    setup: AppointmentSetup,
) -> list[AvailabilityAccessDeniedError]:
    agenda_denials: list[AvailabilityAccessDeniedError] = []

    for raw_actor, raw_tenant in (
        ("", str(setup.organization_id)),
        ("not-a-uuid", str(setup.organization_id)),
        (str(setup.actor_id), ""),
        (str(setup.actor_id), "not-a-uuid"),
    ):
        with runtime_role(), transaction.atomic(), connection.cursor() as cursor:
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
                [raw_tenant],
            )
            cursor.execute(
                "SELECT pg_catalog.set_config('app.current_user_id', %s, true)",
                [raw_actor],
            )
            with pytest.raises(AvailabilityAccessDeniedError) as context_error:
                view_agenda(
                    clinic_id=setup.clinic_id,
                    view="day",
                    date="2035-06-02",
                    page=1,
                )
            agenda_denials.append(context_error.value)
    return agenda_denials


def test_role_union_context_denials_and_object_probing_are_exact(
    rbac_graph: RbacGraph,
) -> None:
    setup, first, second = _seed_two_agendas(rbac_graph)
    appointment_probe_errors = _assert_role_union(setup, first, second)
    agenda_denials = _assert_authorized_input_and_scope_denials(rbac_graph, setup)
    agenda_denials.extend(_assert_raw_context_denials(setup))

    with runtime_role(), tenant_context(setup.actor_id, rbac_graph.organization_b):
        for appointment_id in (first.pk, uuid4()):
            with pytest.raises(AppointmentAccessDeniedError) as appointment_error:
                view_appointment_for_transition(appointment_id=appointment_id)
            appointment_probe_errors.append(appointment_error.value)
    assert {error.args for error in agenda_denials} == {("availability access denied",)}
    assert {error.args for error in appointment_probe_errors} == {
        ("appointment access denied",)
    }

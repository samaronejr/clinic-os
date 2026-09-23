from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import Clinic, User, UserClinicRole
from apps.scheduling.models import Appointment, AvailabilityBlock
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentPractitionerError,
)
from apps.tenancy.db import tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
    seed_cross_clinic_appointment_setups,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.mark.parametrize(
    "role",
    [
        UserClinicRole.Role.OWNER,
        UserClinicRole.Role.CLINIC_ADMIN,
        UserClinicRole.Role.RECEPTIONIST,
    ],
)
def test_each_manager_role_can_create(
    rbac_graph: RbacGraph,
    role: UserClinicRole.Role,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    actor = User.objects.create(
        username=f"todo10-synthetic-manager-{role}-{uuid4().hex}",
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
            user=actor,
            role=role,
        )
    actor_setup = replace(setup, actor_id=actor.pk)

    with runtime_role(), tenant_context(actor.pk, setup.organization_id):
        appointment = create_synthetic_appointment(actor_setup)

    assert appointment.organization_id == setup.organization_id


def test_physician_unassigned_and_foreign_scope_denials_are_indistinguishable(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    denials: list[AppointmentAccessDeniedError] = []
    with (
        runtime_role(),
        tenant_context(setup.practitioner_id, setup.organization_id),
        pytest.raises(AppointmentAccessDeniedError) as physician_error,
    ):
        create_synthetic_appointment(setup)
    denials.append(physician_error.value)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        for denied_setup in (
            replace(setup, clinic_id=rbac_graph.clinic_b),
            replace(setup, clinic_id=rbac_graph.clinic_c),
        ):
            with pytest.raises(AppointmentAccessDeniedError) as error:
                create_synthetic_appointment(denied_setup)
            denials.append(error.value)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            audit_count = cursor.fetchone()

    assert {error.args for error in denials} == {("appointment access denied",)}
    assert audit_count == (0,)


def test_unassigned_practitioner_and_missing_availability_fail_without_residue(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        with pytest.raises(AppointmentPractitionerError):
            create_synthetic_appointment(
                replace(setup, practitioner_id=setup.actor_id),
            )
        with pytest.raises(AppointmentAvailabilityError):
            create_synthetic_appointment(
                setup,
                start_local="2035-06-02T07:00",
                end_local="2035-06-02T08:00",
            )
        block = AvailabilityBlock.objects.get(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
        )
        block.retired_at = block.created_at
        block.save(update_fields=("retired_at", "updated_at"))
        with pytest.raises(AppointmentAvailabilityError):
            create_synthetic_appointment(setup)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            audit_count = cursor.fetchone()

    assert audit_count == (0,)


def test_cross_clinic_enrollment_probe_is_denied_after_manager_authorization(
    rbac_graph: RbacGraph,
) -> None:
    first, second = seed_cross_clinic_appointment_setups(rbac_graph)
    forged = replace(second, enrollment_id=first.enrollment_id)
    with (
        runtime_role(),
        tenant_context(second.actor_id, second.organization_id),
        pytest.raises(AppointmentAccessDeniedError),
    ):
        create_synthetic_appointment(forged)

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(first.organization_id)],
        )
        assert Appointment.objects.count() == 0


def test_strict_local_range_rejects_past_cross_midnight_and_dst_fold(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        Clinic.objects.filter(pk=setup.clinic_id).update(timezone="America/New_York")
    invalid_ranges = (
        ("2020-01-01T09:00", "2020-01-01T10:00"),
        ("2035-06-02T23:30", "2035-06-03T00:30"),
        ("2035-11-04T01:30", "2035-11-04T02:30"),
    )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        for start_local, end_local in invalid_ranges:
            with pytest.raises(AppointmentCreateInputError):
                create_synthetic_appointment(
                    setup,
                    start_local=start_local,
                    end_local=end_local,
                )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            audit_count = cursor.fetchone()

    assert audit_count == (0,)

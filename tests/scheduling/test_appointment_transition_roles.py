from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import User, UserClinicRole
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    AppointmentAccessDeniedError,
    AppointmentLocalRange,
    cancel_appointment,
    reschedule_appointment,
)
from apps.tenancy.db import TenantAccessDeniedError, tenant_context
from django.contrib.auth.hashers import make_password
from django.db import connection, transaction

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
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
def test_each_manager_role_can_apply_transitions(
    rbac_graph: RbacGraph,
    role: UserClinicRole.Role,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    actor = User.objects.create(
        username=f"todo11-synthetic-manager-{role}-{uuid4().hex}",
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
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    with runtime_role(), tenant_context(actor.pk, setup.organization_id):
        moved = reschedule_appointment(
            appointment_id=appointment.pk,
            local_range=AppointmentLocalRange(
                "2035-06-02T10:00",
                "2035-06-02T11:00",
            ),
        )
        cancelled = cancel_appointment(
            appointment_id=appointment.pk,
            reason=Appointment.CancellationReason.OTHER,
        )

    assert moved.pk == appointment.pk
    assert cancelled.status == Appointment.Status.CANCELLED


def test_physician_unassigned_cross_clinic_and_cross_org_denials_match(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    no_membership = User.objects.get(username=rbac_graph.no_membership_username)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
    denials: list[AppointmentAccessDeniedError] = []
    for actor_id, organization_id in (
        (setup.practitioner_id, setup.organization_id),
        (rbac_graph.clinic_admin, setup.organization_id),
        (rbac_graph.shared_user, rbac_graph.organization_b),
    ):
        with runtime_role(), tenant_context(actor_id, organization_id):
            with pytest.raises(AppointmentAccessDeniedError) as cancel_error:
                cancel_appointment(
                    appointment_id=appointment.pk,
                    reason=Appointment.CancellationReason.OTHER,
                )
            denials.append(cancel_error.value)
            with pytest.raises(AppointmentAccessDeniedError) as reschedule_error:
                reschedule_appointment(
                    appointment_id=appointment.pk,
                    local_range=AppointmentLocalRange(
                        "2035-06-02T10:00",
                        "2035-06-02T11:00",
                    ),
                )
            denials.append(reschedule_error.value)
    with (
        runtime_role(),
        pytest.raises(TenantAccessDeniedError),
        tenant_context(no_membership.pk, setup.organization_id),
    ):
        pytest.fail("unassigned actor unexpectedly entered tenant context")

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment.refresh_from_db()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type IN ("
                "'scheduling.appointment.rescheduled', "
                "'scheduling.appointment.cancelled')"
            )
            transition_events = cursor.fetchone()

    assert {error.args for error in denials} == {("appointment access denied",)}
    assert appointment.status == Appointment.Status.SCHEDULED
    assert transition_events == (0,)

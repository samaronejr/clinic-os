from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.identity.models import UserClinicRole
from apps.scheduling.models import Appointment, AvailabilityBlock
from apps.scheduling.services import (
    AppointmentAvailabilityError,
    AppointmentIdempotencyConflictError,
    create_availability,
    retire_availability,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.utils import timezone

from patient_service_support import runtime_role
from scheduling.appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_equal_replay_returns_current_row_after_transition_and_role_removal(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        original = create_synthetic_appointment(setup, idempotency_key=key)
    shifted_start = original.start_at + timedelta(hours=1)
    shifted_end = original.end_at + timedelta(hours=1)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        Appointment.objects.filter(pk=original.pk).update(
            start_at=shifted_start,
            end_at=shifted_end,
            status=Appointment.Status.CANCELLED,
            cancellation_reason=Appointment.CancellationReason.CLINIC_REQUEST,
            cancelled_at=timezone.now(),
        )
        UserClinicRole.objects.filter(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user_id=setup.practitioner_id,
            role=UserClinicRole.Role.PHYSICIAN,
        ).delete()
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        replay = create_synthetic_appointment(setup, idempotency_key=key)
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            created_events = cursor.fetchone()

    assert replay.pk == original.pk
    assert replay.status == Appointment.Status.CANCELLED
    assert replay.start_at == shifted_start
    assert replay.end_at == shifted_end
    assert len(bytes(replay.create_fingerprint)) == 32
    assert created_events == (1,)


def test_mismatched_replay_conflicts_without_new_row_or_audit(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        original = create_synthetic_appointment(setup, idempotency_key=key)
        with pytest.raises(AppointmentIdempotencyConflictError):
            create_synthetic_appointment(
                setup,
                idempotency_key=key,
                start_local="2035-06-02T09:15",
            )
        assert Appointment.objects.count() == 1
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            created_events = cursor.fetchone()

    assert original.idempotency_key == key
    assert created_events == (1,)


def test_failed_first_attempt_leaves_the_key_reusable(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    key = uuid4()
    with (
        runtime_role(),
        tenant_context(setup.actor_id, setup.organization_id),
    ):
        block = AvailabilityBlock.objects.get(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
        )
        retire_availability(
            clinic_id=setup.clinic_id,
            availability_id=block.pk,
        )
        with pytest.raises(AppointmentAvailabilityError):
            create_synthetic_appointment(setup, idempotency_key=key)
        create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=setup.practitioner_id,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
        accepted = create_synthetic_appointment(setup, idempotency_key=key)
        appointment_count = Appointment.objects.count()
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.appointment.created'"
            )
            created_events = cursor.fetchone()

    assert accepted.idempotency_key == key
    assert appointment_count == 1
    assert created_events == (1,)

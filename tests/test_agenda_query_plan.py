from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING
from uuid import uuid4

import pytest
from apps.scheduling.models import Appointment
from apps.scheduling.services import (
    prepare_booking,
    view_agenda,
    view_appointment_for_transition,
)
from apps.tenancy.db import tenant_context
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from appointment_service_support import (
    create_synthetic_appointment,
    seed_appointment_setup,
)
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


def test_query_counts_user_joins_and_volume_plan_stay_bounded(
    rbac_graph: RbacGraph,
) -> None:
    setup = seed_appointment_setup(rbac_graph)
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        appointment = create_synthetic_appointment(setup)
        with CaptureQueriesContext(connection) as booking_queries:
            prepare_booking(
                clinic_id=setup.clinic_id,
                enrollment_id=setup.enrollment_id,
            )
        with CaptureQueriesContext(connection) as appointment_queries:
            view_appointment_for_transition(appointment_id=appointment.pk)
        with CaptureQueriesContext(connection) as agenda_queries:
            view_agenda(
                clinic_id=setup.clinic_id,
                view="day",
                date="2035-06-02",
                page=1,
            )

        # Each fetched patient row decrypts its two envelope columns
        # (full_name, birth_date) through protected_decrypt, one call each.
        assert len(booking_queries) <= 16
        assert len(appointment_queries) <= 15
        assert len(agenda_queries) <= 16
        statements = "\n".join(
            query["sql"]
            for captured in (booking_queries, appointment_queries, agenda_queries)
            for query in captured.captured_queries
        )
        assert 'FROM "identity_user"' not in statements
        assert 'JOIN "identity_user"' not in statements
        assert "clinic_app.list_active_clinic_physicians" in statements

        cancelled_at = timezone.now()
        first_day = datetime(2036, 1, 1, 12, 0, tzinfo=UTC)
        Appointment.objects.bulk_create(
            [
                Appointment(
                    organization_id=setup.organization_id,
                    clinic_id=setup.clinic_id,
                    patient_id=setup.patient_id,
                    practitioner_id=setup.practitioner_id,
                    start_at=first_day + timedelta(days=index),
                    end_at=first_day + timedelta(days=index, minutes=30),
                    idempotency_key=uuid4(),
                    create_fingerprint=b"q" * 32,
                    status=Appointment.Status.CANCELLED,
                    cancellation_reason=Appointment.CancellationReason.OTHER,
                    cancelled_at=cancelled_at,
                )
                for index in range(1200)
            ],
            batch_size=200,
        )

    # The plan assertion is only meaningful at volume: refresh the table
    # statistics as the owner so the planner sees the 1200-row bulk insert
    # instead of whatever stale estimate earlier tests left behind.
    with connection.cursor() as cursor:
        cursor.execute("ANALYZE clinic_app.scheduling_appointment")

    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        plan = (
            Appointment.objects.filter(
                organization_id=setup.organization_id,
                clinic_id=setup.clinic_id,
                start_at__gte=datetime(2037, 5, 1, tzinfo=UTC),
                start_at__lt=datetime(2037, 5, 2, tzinfo=UTC),
                status__in=(
                    Appointment.Status.SCHEDULED,
                    Appointment.Status.CANCELLED,
                ),
            )
            .order_by("start_at", "practitioner_id", "pk")
            .explain()
        )

    assert "scheduling_appointment_clinic_start_idx" in plan

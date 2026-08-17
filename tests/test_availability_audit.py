from __future__ import annotations

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

import pytest
from apps.audit.services import record_phase1_event, verify_chain
from apps.core.idempotency import create_fingerprint
from apps.identity.models import UserClinicRole
from apps.intake.services import create_patient
from apps.scheduling import services
from apps.scheduling.models import Appointment, AvailabilityBlock
from apps.tenancy.db import tenant_context
from django.db import connection, transaction
from django.utils import timezone

from availability_service_support import create_synthetic_block
from patient_service_support import runtime_role

if TYPE_CHECKING:
    from conftest import RbacGraph

pytestmark = pytest.mark.django_db(transaction=True)


class ForcedAvailabilityAuditFailureError(Exception):
    pass


def test_equal_create_replay_survives_retirement_and_target_role_removal(
    rbac_graph: RbacGraph,
) -> None:
    idempotency_key = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        first = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-01-04", "09:00", "10:00"),
            idempotency_key=idempotency_key,
        )
        services.retire_availability(
            clinic_id=rbac_graph.clinic_a,
            availability_id=first.pk,
        )

    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.filter(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            user_id=rbac_graph.physician,
            role=UserClinicRole.Role.PHYSICIAN,
        ).delete()

    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        replay = services.create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-01-04T09:00",
            end_local="2035-01-04T10:00",
            idempotency_key=idempotency_key,
        )
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM clinic_app.audit_event_tenant")
            audit_count = cursor.fetchone()

    assert replay.pk == first.pk
    assert replay.retired_at is not None
    assert audit_count == (2,)


def test_retirement_refuses_a_future_appointment_then_succeeds_after_cancellation(
    rbac_graph: RbacGraph,
) -> None:
    appointment_error = getattr(services, "AvailabilityHasAppointmentsError", None)
    assert isinstance(appointment_error, type)
    assert issubclass(appointment_error, Exception)
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        block = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-03-01", "09:00", "12:00"),
        )
        registration = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Synthetic Appointment Holder",
            birth_date=date(2000, 1, 2),
            idempotency_key=uuid4(),
        )
        appointment_key = uuid4()
        appointment = Appointment.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_a,
            patient_id=registration.patient.pk,
            practitioner_id=rbac_graph.physician,
            start_at=datetime(2035, 3, 1, 12, 30, tzinfo=UTC),
            end_at=datetime(2035, 3, 1, 13, 0, tzinfo=UTC),
            idempotency_key=appointment_key,
            create_fingerprint=create_fingerprint(
                "appointment",
                {
                    "clinic_id": str(rbac_graph.clinic_a),
                    "end_utc": "2035-03-01T13:00:00Z",
                    "enrollment_id": str(registration.enrollment.pk),
                    "practitioner_id": str(rbac_graph.physician),
                    "start_utc": "2035-03-01T12:30:00Z",
                },
            ),
        )
        with pytest.raises(appointment_error, match="future appointments"):
            services.retire_availability(
                clinic_id=rbac_graph.clinic_a,
                availability_id=block.pk,
            )
        assert AvailabilityBlock.objects.get(pk=block.pk).retired_at is None
        appointment.status = Appointment.Status.CANCELLED
        appointment.cancellation_reason = Appointment.CancellationReason.CLINIC_REQUEST
        appointment.cancelled_at = timezone.now()
        appointment.save(
            update_fields=(
                "status",
                "cancellation_reason",
                "cancelled_at",
                "updated_at",
            )
        )
        retired = services.retire_availability(
            clinic_id=rbac_graph.clinic_a,
            availability_id=block.pk,
        )
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT count(*) FROM clinic_app.audit_event_tenant "
                "WHERE event_type = 'scheduling.availability.retired'"
            )
            retired_audit_count = cursor.fetchone()

    assert retired.retired_at is not None
    assert retired_audit_count == (1,)


def test_failed_audit_appends_roll_back_and_leave_create_key_reusable(
    rbac_graph: RbacGraph,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_append = record_phase1_event

    def append_then_fail(
        event_type: str,
        *,
        clinic_id: UUID,
        affected_record_id: UUID,
    ) -> int:
        original_append(
            event_type,
            clinic_id=clinic_id,
            affected_record_id=affected_record_id,
        )
        raise ForcedAvailabilityAuditFailureError

    key = uuid4()
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        monkeypatch.setattr(
            "apps.scheduling.availability_creation.record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedAvailabilityAuditFailureError):
            create_synthetic_block(
                rbac_graph.clinic_a,
                rbac_graph.physician,
                ("2035-03-02", "09:00", "10:00"),
                idempotency_key=key,
            )
        monkeypatch.setattr(
            "apps.scheduling.availability_creation.record_phase1_event",
            original_append,
        )
        block = create_synthetic_block(
            rbac_graph.clinic_a,
            rbac_graph.physician,
            ("2035-03-02", "09:00", "10:00"),
            idempotency_key=key,
        )
        monkeypatch.setattr(
            "apps.scheduling.availability_retirement.record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedAvailabilityAuditFailureError):
            services.retire_availability(
                clinic_id=rbac_graph.clinic_a,
                availability_id=block.pk,
            )
        monkeypatch.setattr(
            "apps.scheduling.availability_view.record_phase1_event",
            append_then_fail,
        )
        with pytest.raises(ForcedAvailabilityAuditFailureError):
            services.view_availability(clinic_id=rbac_graph.clinic_a)
        stored = AvailabilityBlock.objects.get(pk=block.pk)
        with connection.cursor() as cursor:
            cursor.execute("SELECT event_type FROM clinic_app.audit_event_tenant")
            event_types = cursor.fetchall()
        verification = verify_chain(rbac_graph.organization_a)

    assert stored.retired_at is None
    assert event_types == [("scheduling.availability.created",)]
    assert verification.valid is True
    assert verification.row_count == 1

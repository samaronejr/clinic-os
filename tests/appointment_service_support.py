from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from apps.identity.models import UserClinicRole
from apps.intake.models import PatientClinicEnrollment
from apps.intake.services import create_patient
from apps.scheduling.services import (
    AppointmentLocalRange,
    create_appointment,
    create_availability,
)
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from apps.scheduling.models import Appointment

    from conftest import RbacGraph


@dataclass(frozen=True, slots=True)
class AppointmentSetup:
    organization_id: UUID
    clinic_id: UUID
    actor_id: UUID
    practitioner_id: UUID
    enrollment_id: UUID
    patient_id: UUID


def seed_appointment_setup(rbac_graph: RbacGraph) -> AppointmentSetup:
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        registration = create_patient(
            clinic_id=rbac_graph.clinic_a,
            full_name="Synthetic Booking Persona",
            birth_date=date(2000, 1, 2),
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=rbac_graph.clinic_a,
            practitioner_id=rbac_graph.physician,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
    return AppointmentSetup(
        organization_id=rbac_graph.organization_a,
        clinic_id=rbac_graph.clinic_a,
        actor_id=rbac_graph.shared_user,
        practitioner_id=rbac_graph.physician,
        enrollment_id=registration.enrollment.pk,
        patient_id=registration.patient.pk,
    )


def seed_cross_clinic_appointment_setups(
    rbac_graph: RbacGraph,
) -> tuple[AppointmentSetup, AppointmentSetup]:
    first = seed_appointment_setup(rbac_graph)
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_catalog.set_config('app.current_tenant', %s, true)",
            [str(rbac_graph.organization_a)],
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.shared_user,
            role=UserClinicRole.Role.RECEPTIONIST,
        )
        UserClinicRole.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            user_id=rbac_graph.clinic_admin,
            role=UserClinicRole.Role.PHYSICIAN,
        )
    with (
        runtime_role(),
        tenant_context(rbac_graph.shared_user, rbac_graph.organization_a),
    ):
        enrollment = PatientClinicEnrollment.objects.create(
            organization_id=rbac_graph.organization_a,
            clinic_id=rbac_graph.clinic_b,
            patient_id=first.patient_id,
            idempotency_key=uuid4(),
            create_fingerprint=b"s" * 32,
        )
        create_availability(
            clinic_id=rbac_graph.clinic_b,
            practitioner_id=rbac_graph.clinic_admin,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
    second = AppointmentSetup(
        organization_id=rbac_graph.organization_a,
        clinic_id=rbac_graph.clinic_b,
        actor_id=rbac_graph.shared_user,
        practitioner_id=rbac_graph.clinic_admin,
        enrollment_id=enrollment.pk,
        patient_id=first.patient_id,
    )
    return first, second


def create_synthetic_appointment(
    setup: AppointmentSetup,
    *,
    idempotency_key: UUID | None = None,
    start_local: str = "2035-06-02T09:00",
    end_local: str = "2035-06-02T10:00",
) -> Appointment:
    return create_appointment(
        clinic_id=setup.clinic_id,
        enrollment_id=setup.enrollment_id,
        practitioner_id=setup.practitioner_id,
        local_range=AppointmentLocalRange(start_local, end_local),
        idempotency_key=idempotency_key or uuid4(),
    )

from __future__ import annotations

from dataclasses import replace
from datetime import date
from typing import TYPE_CHECKING
from uuid import uuid4

from apps.identity.models import User, UserClinicRole
from apps.intake.services import create_patient
from apps.scheduling.services import create_availability
from apps.tenancy.db import tenant_context
from django.db import connection, transaction

from patient_service_support import runtime_role

if TYPE_CHECKING:
    from scheduling.appointment_service_support import AppointmentSetup


def independent_setup(setup: AppointmentSetup, index: int) -> AppointmentSetup:
    with transaction.atomic(), connection.cursor() as cursor:
        cursor.execute(
            "SELECT set_config('app.current_tenant', %s, true)",
            [str(setup.organization_id)],
        )
        physician = User.objects.create(
            username=f"sintetico-resource-{index}-{uuid4()}",
            email=f"{uuid4()}@example.invalid",
            is_active=True,
        )
        UserClinicRole.objects.create(
            organization_id=setup.organization_id,
            clinic_id=setup.clinic_id,
            user=physician,
            role="physician",
        )
    with runtime_role(), tenant_context(setup.actor_id, setup.organization_id):
        patient = create_patient(
            clinic_id=setup.clinic_id,
            full_name=f"Sintetico resource patient {index}",
            birth_date=date(2000, 1, 1),
            idempotency_key=uuid4(),
        )
        create_availability(
            clinic_id=setup.clinic_id,
            practitioner_id=physician.pk,
            start_local="2035-06-02T08:00",
            end_local="2035-06-02T12:00",
            idempotency_key=uuid4(),
        )
    return replace(
        setup,
        practitioner_id=physician.pk,
        enrollment_id=patient.enrollment.pk,
        patient_id=patient.patient.pk,
    )

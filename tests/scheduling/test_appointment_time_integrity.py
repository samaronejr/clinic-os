from datetime import UTC, date, datetime
from uuid import UUID

import pytest
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.scheduling.models import Appointment, AvailabilityBlock
from django.db import IntegrityError, transaction

from scheduling.availability_test_support import seed


@pytest.mark.django_db(transaction=True)
def test_scheduled_appointment_is_future_and_one_clinic_local_date() -> None:
    organization, clinic, _, practitioner, _ = seed()
    assert clinic.timezone == "America/Sao_Paulo"
    patient = Patient.objects.create(
        organization=organization,
        full_name="Synthetic Time Boundary Patient",
        birth_date=date(2000, 1, 2),
    )
    PatientClinicEnrollment.objects.create(
        organization=organization,
        clinic=clinic,
        patient=patient,
        idempotency_key=UUID(int=7301),
        create_fingerprint=b"a" * 32,
    )
    past_start = datetime(2020, 1, 2, 14, tzinfo=UTC)
    past_end = datetime(2020, 1, 2, 15, tzinfo=UTC)
    overnight_start = datetime(2030, 1, 3, 2, 30, tzinfo=UTC)
    overnight_end = datetime(2030, 1, 3, 3, 30, tzinfo=UTC)
    for offset, (start_at, end_at) in enumerate(
        ((past_start, past_end), (overnight_start, overnight_end)),
        start=1,
    ):
        AvailabilityBlock.objects.create(
            organization=organization,
            clinic=clinic,
            practitioner=practitioner,
            start_at=start_at,
            end_at=end_at,
            idempotency_key=UUID(int=7310 + offset),
            create_fingerprint=bytes([offset]) * 32,
        )
        with pytest.raises(IntegrityError), transaction.atomic():
            Appointment.objects.create(
                organization=organization,
                clinic=clinic,
                patient=patient,
                practitioner=practitioner,
                start_at=start_at,
                end_at=end_at,
                idempotency_key=UUID(int=7320 + offset),
                create_fingerprint=bytes([offset + 2]) * 32,
            )

from datetime import date, timedelta
from uuid import UUID

import pytest
from apps.identity.models import User
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.scheduling.models import Appointment, AvailabilityBlock
from django.db import IntegrityError, transaction
from django.utils import timezone

from scheduling.availability_test_support import END, START, seed


@pytest.mark.django_db(transaction=True)
def test_appointment_requires_enrollment_and_active_same_clinic_availability() -> None:
    organization, clinic_a, clinic_b, practitioner_a, practitioner_b = seed()
    practitioner_without_availability = User.objects.create(
        id=UUID(int=7101),
        username="synthetic-practitioner-without-availability",
    )
    patient = Patient.objects.create(
        organization=organization,
        full_name="Synthetic Eligibility Patient",
        birth_date=date(2000, 1, 2),
    )
    PatientClinicEnrollment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        idempotency_key=UUID(int=7102),
        create_fingerprint=b"a" * 32,
    )
    for offset, (clinic, practitioner) in enumerate(
        ((clinic_a, practitioner_a), (clinic_b, practitioner_b)),
        start=1,
    ):
        AvailabilityBlock.objects.create(
            organization=organization,
            clinic=clinic,
            practitioner=practitioner,
            start_at=START,
            end_at=END,
            idempotency_key=UUID(int=7110 + offset),
            create_fingerprint=bytes([offset]) * 32,
        )

    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.create(
            organization=organization,
            clinic=clinic_b,
            patient=patient,
            practitioner=practitioner_b,
            start_at=START,
            end_at=END,
            idempotency_key=UUID(int=7121),
            create_fingerprint=b"b" * 32,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.create(
            organization=organization,
            clinic=clinic_a,
            patient=patient,
            practitioner=practitioner_without_availability,
            start_at=START,
            end_at=END,
            idempotency_key=UUID(int=7122),
            create_fingerprint=b"c" * 32,
        )

    AvailabilityBlock.objects.filter(
        clinic=clinic_a,
        practitioner=practitioner_a,
    ).update(retired_at=timezone.now())
    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.create(
            organization=organization,
            clinic=clinic_a,
            patient=patient,
            practitioner=practitioner_a,
            start_at=START,
            end_at=END,
            idempotency_key=UUID(int=7123),
            create_fingerprint=b"d" * 32,
        )

    active = AvailabilityBlock.objects.create(
        organization=organization,
        clinic=clinic_a,
        practitioner=practitioner_without_availability,
        start_at=START - timedelta(hours=1),
        end_at=END + timedelta(hours=1),
        idempotency_key=UUID(int=7124),
        create_fingerprint=b"e" * 32,
    )
    appointment = Appointment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        practitioner=practitioner_without_availability,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7125),
        create_fingerprint=b"f" * 32,
    )
    assert active.retired_at is None
    assert appointment.status == Appointment.Status.SCHEDULED

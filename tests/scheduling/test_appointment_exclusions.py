from datetime import date, timedelta
from importlib.util import find_spec
from uuid import UUID

import pytest
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.scheduling.models import Appointment, AvailabilityBlock
from django.db import IntegrityError, transaction
from django.utils import timezone

from scheduling.availability_test_support import END, ORG_ID, START, seed


@pytest.mark.django_db(transaction=True)
def test_scheduled_overlap_exclusions_use_half_open_ranges() -> None:
    assert find_spec("apps.scheduling.migrations.0002_appointment") is not None
    organization, clinic_a, clinic_b, practitioner_a, practitioner_b = seed()
    patient = Patient.objects.create(
        organization=organization,
        full_name="Synthetic Appointment Patient",
        birth_date=date(2000, 1, 2),
    )
    other_patient = Patient.objects.create(
        organization=organization,
        full_name="Synthetic Appointment Patient Two",
        birth_date=date(2001, 2, 3),
    )
    for offset, (clinic, enrolled_patient) in enumerate(
        (
            (clinic_a, patient),
            (clinic_b, patient),
            (clinic_a, other_patient),
        ),
        start=1,
    ):
        PatientClinicEnrollment.objects.create(
            organization=organization,
            clinic=clinic,
            patient=enrolled_patient,
            idempotency_key=UUID(int=7000 + offset),
            create_fingerprint=bytes([offset]) * 32,
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
            end_at=END + timedelta(hours=2),
            idempotency_key=UUID(int=7010 + offset),
            create_fingerprint=bytes([offset + 3]) * 32,
        )

    first = Appointment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        practitioner=practitioner_a,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7021),
        create_fingerprint=b"a" * 32,
    )
    Appointment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        practitioner=practitioner_a,
        start_at=END,
        end_at=END + timedelta(hours=1),
        idempotency_key=UUID(int=7022),
        create_fingerprint=b"b" * 32,
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.create(
            organization=organization,
            clinic=clinic_a,
            patient=other_patient,
            practitioner=practitioner_a,
            start_at=START + timedelta(minutes=15),
            end_at=END - timedelta(minutes=15),
            idempotency_key=UUID(int=7023),
            create_fingerprint=b"c" * 32,
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.create(
            organization=organization,
            clinic=clinic_b,
            patient=patient,
            practitioner=practitioner_b,
            start_at=START + timedelta(minutes=15),
            end_at=END - timedelta(minutes=15),
            idempotency_key=UUID(int=7024),
            create_fingerprint=b"d" * 32,
        )

    Appointment.objects.filter(pk=first.pk).update(
        status=Appointment.Status.CANCELLED,
        cancellation_reason=Appointment.CancellationReason.PATIENT_REQUEST,
        cancelled_at=timezone.now(),
    )
    replacement = Appointment.objects.create(
        organization=organization,
        clinic=clinic_a,
        patient=patient,
        practitioner=practitioner_a,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7025),
        create_fingerprint=b"e" * 32,
    )
    assert replacement.status == Appointment.Status.SCHEDULED
    assert Appointment.objects.count() == 3
    assert Appointment.objects.filter(organization_id=ORG_ID).count() == 3

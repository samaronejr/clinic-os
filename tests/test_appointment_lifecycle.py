from datetime import date, timedelta
from uuid import UUID

import pytest
from apps.intake.models import Patient, PatientClinicEnrollment
from apps.scheduling.models import Appointment, AvailabilityBlock
from django.db import IntegrityError, transaction
from django.utils import timezone

from availability_test_support import END, START, seed


@pytest.mark.django_db(transaction=True)
def test_appointment_checks_range_fingerprint_lifecycle_and_terminal_state() -> None:
    organization, clinic, _, practitioner, _ = seed()
    patient = Patient.objects.create(
        organization=organization,
        full_name="Synthetic Lifecycle Patient",
        birth_date=date(2000, 1, 2),
    )
    PatientClinicEnrollment.objects.create(
        organization=organization,
        clinic=clinic,
        patient=patient,
        idempotency_key=UUID(int=7201),
        create_fingerprint=b"a" * 32,
    )
    AvailabilityBlock.objects.create(
        organization=organization,
        clinic=clinic,
        practitioner=practitioner,
        start_at=START,
        end_at=END + timedelta(hours=3),
        idempotency_key=UUID(int=7202),
        create_fingerprint=b"b" * 32,
    )
    first = Appointment.objects.create(
        organization=organization,
        clinic=clinic,
        patient=patient,
        practitioner=practitioner,
        start_at=START,
        end_at=END,
        idempotency_key=UUID(int=7203),
        create_fingerprint=b"c" * 32,
    )

    invalid_rows = (
        {
            "status": Appointment.Status.CANCELLED,
            "start_at": END + timedelta(hours=1),
            "end_at": END + timedelta(hours=2),
            "create_fingerprint": b"d" * 32,
        },
        {
            "status": Appointment.Status.SCHEDULED,
            "cancellation_reason": Appointment.CancellationReason.DUPLICATE,
            "start_at": END + timedelta(hours=1),
            "end_at": END + timedelta(hours=2),
            "create_fingerprint": b"e" * 32,
        },
        {
            "status": Appointment.Status.SCHEDULED,
            "start_at": END + timedelta(seconds=1),
            "end_at": END + timedelta(hours=1),
            "create_fingerprint": b"f" * 32,
        },
        {
            "status": Appointment.Status.SCHEDULED,
            "start_at": END + timedelta(hours=2),
            "end_at": END + timedelta(hours=2),
            "create_fingerprint": b"g" * 32,
        },
        {
            "status": Appointment.Status.SCHEDULED,
            "start_at": END + timedelta(hours=2),
            "end_at": END + timedelta(hours=3),
            "create_fingerprint": b"short",
        },
    )
    for offset, values in enumerate(invalid_rows, start=1):
        with pytest.raises(IntegrityError), transaction.atomic():
            Appointment.objects.create(
                organization=organization,
                clinic=clinic,
                patient=patient,
                practitioner=practitioner,
                idempotency_key=UUID(int=7210 + offset),
                **values,
            )

    Appointment.objects.filter(pk=first.pk).update(
        status=Appointment.Status.CANCELLED,
        cancellation_reason=Appointment.CancellationReason.CLINIC_REQUEST,
        cancelled_at=timezone.now(),
    )
    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.filter(pk=first.pk).update(
            start_at=END,
            end_at=END + timedelta(hours=1),
        )
    with pytest.raises(IntegrityError), transaction.atomic():
        Appointment.objects.filter(pk=first.pk).delete()

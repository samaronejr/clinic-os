"""FIFO opening offers with patient-only, atomic shared-service acceptance.

The queue lock orders offers, not bookings. Only create_appointment can reserve
an opening. Notice eligibility is re-read, never treated as a delivery receipt.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING, Final

from django.db import connection, models, transaction
from django.utils import timezone

from apps.identity.current_context import practitioner_display_label
from apps.intake.models import (
    PatientChannelPreference,
    PatientClinicEnrollment,
    PatientContact,
)
from apps.scheduling.access import (
    AppointmentAccessDeniedError,
    authorized_appointment_manager_clinic,
)
from apps.scheduling.appointment_values import require_active_practitioner
from apps.scheduling.models import (
    Appointment,
    AvailabilityBlock,
    WaitlistEntry,
    WaitlistOffer,
)
from apps.scheduling.patient_authority import require_patient_booking_scope
from apps.scheduling.services import (
    AppointmentAvailabilityError,
    AppointmentCreateInputError,
    AppointmentIdempotencyConflictError,
    AppointmentLocalRange,
    AppointmentPractitionerError,
    SlotConflict,
    create_appointment,
)
from apps.scheduling.timezones import format_local_minute, parse_local_minute

if TYPE_CHECKING:
    from datetime import datetime
    from uuid import UUID

    from apps.identity.models import Clinic
    from apps.scheduling.patient_authority import PatientBookingScope

OFFER_LIFETIME: Final = timedelta(minutes=30)


class WaitlistInputError(ValueError):
    """Reject invalid windows and unavailable openings without reflecting input."""


class _ExpiredDuringBookingError(Exception):
    """Roll back a booking whose offer elapsed while waiting for slot locks."""


def _window(clinic: Clinic, start: str, end: str) -> tuple[datetime, datetime]:
    try:
        starts = parse_local_minute(start, clinic.timezone or "")
        ends = parse_local_minute(end, clinic.timezone or "")
    except ValueError as error:
        raise WaitlistInputError from error
    if starts <= timezone.now() or ends <= starts:
        raise WaitlistInputError
    return starts, ends


def _lock(clinic_id: UUID, practitioner_id: UUID) -> None:
    # Separate namespace from appointment locks: no offer reserves a slot.
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))",
            [f"waitlist:{clinic_id}:{practitioner_id}"],
        )


def add_waitlist_entry(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    practitioner_id: UUID,
    start_local: str,
    end_local: str,
) -> WaitlistEntry:
    """Record a staff-authorized request with an immutable FIFO sequence."""
    with transaction.atomic():
        clinic = authorized_appointment_manager_clinic(clinic_id)
        starts, ends = _window(clinic, start_local, end_local)
        require_active_practitioner(clinic_id, practitioner_id)
        try:
            enrollment = PatientClinicEnrollment.objects.get(
                pk=enrollment_id,
                organization_id=clinic.organization_id,
                clinic_id=clinic_id,
            )
        except PatientClinicEnrollment.DoesNotExist as error:
            raise AppointmentAccessDeniedError from error
        _lock(clinic_id, practitioner_id)
        # Browser retries must not give one patient duplicate active queue places.
        existing = WaitlistEntry.objects.filter(
            enrollment_id=enrollment_id,
            practitioner_id=practitioner_id,
            start_at=starts,
            end_at=ends,
            state__in=["waiting", "offered"],
        ).first()
        if existing is not None:
            return existing
        return WaitlistEntry.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            patient_id=enrollment.patient_id,
            enrollment_id=enrollment_id,
            practitioner_id=practitioner_id,
            practitioner_label=practitioner_display_label(practitioner_id, clinic_id),
            start_at=starts,
            end_at=ends,
        )


def _finish(offer: WaitlistOffer, state: str, entry_state: str) -> WaitlistOffer:
    offer.state = state
    offer.responded_at = timezone.now()
    offer.save(update_fields=("state", "responded_at", "appointment"))
    WaitlistEntry.objects.filter(pk=offer.entry_id).update(state=entry_state)
    return offer


def _expire(offer: WaitlistOffer) -> bool:
    if offer.state == "pending" and offer.expires_at <= timezone.now():
        _finish(offer, "expired", "expired")
        return True
    return False


def _expire_practitioner(clinic_id: UUID, practitioner_id: UUID) -> None:
    for offer in (
        WaitlistOffer.objects.select_for_update()
        .filter(
            clinic_id=clinic_id,
            practitioner_id=practitioner_id,
            state="pending",
            expires_at__lte=timezone.now(),
        )
        .order_by("pk")
    ):
        _expire(offer)


def issue_waitlist_offer(
    *,
    clinic_id: UUID,
    practitioner_id: UUID,
    start_local: str,
    end_local: str,
) -> WaitlistOffer | None:
    """Offer an opening to the first matching eligible request, exactly once."""
    with transaction.atomic():
        clinic = authorized_appointment_manager_clinic(clinic_id)
        starts, ends = _window(clinic, start_local, end_local)
        if start_local[:10] != end_local[:10]:
            raise WaitlistInputError
        require_active_practitioner(clinic_id, practitioner_id)
        _lock(clinic_id, practitioner_id)
        _expire_practitioner(clinic_id, practitioner_id)
        pending = WaitlistOffer.objects.filter(
            practitioner_id=practitioner_id,
            state="pending",
            start_at__lt=ends,
            end_at__gt=starts,
        ).first()
        if pending is not None:
            return pending
        if (
            not AvailabilityBlock.objects.filter(
                clinic_id=clinic_id,
                practitioner_id=practitioner_id,
                retired_at__isnull=True,
                start_at__lte=starts,
                end_at__gte=ends,
            ).exists()
            or Appointment.objects.filter(
                practitioner_id=practitioner_id,
                status="scheduled",
                start_at__lt=ends,
                end_at__gt=starts,
            ).exists()
        ):
            raise WaitlistInputError
        busy_patients = Appointment.objects.filter(
            status="scheduled",
            start_at__lt=ends,
            end_at__gt=starts,
        ).values("patient_id")
        entry = (
            WaitlistEntry.objects.select_for_update()
            .filter(
                clinic_id=clinic_id,
                practitioner_id=practitioner_id,
                state="waiting",
                start_at__lte=starts,
                end_at__gte=ends,
            )
            .exclude(patient_id__in=busy_patients)
            .order_by("pk")
            .first()
        )
        if entry is None:
            return None
        now = timezone.now()
        offer = WaitlistOffer.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic_id,
            practitioner_id=practitioner_id,
            entry=entry,
            start_at=starts,
            end_at=ends,
            created_at=now,
            expires_at=now + OFFER_LIFETIME,
        )
        entry.state = WaitlistEntry.State.OFFERED
        entry.save(update_fields=("state",))
        return offer


def staff_waitlist(clinic_id: UUID) -> tuple[WaitlistEntry, ...]:
    """Show the authorized queue and preserved offer history, expiring lazily."""
    with transaction.atomic():
        authorized_appointment_manager_clinic(clinic_id)
        practitioners = (
            WaitlistEntry.objects.filter(clinic_id=clinic_id)
            .values_list("practitioner_id", flat=True)
            .distinct()
            .order_by("practitioner_id")
        )
        for practitioner_id in practitioners:
            _lock(clinic_id, practitioner_id)
            _expire_practitioner(clinic_id, practitioner_id)
        return tuple(
            WaitlistEntry.objects.filter(clinic_id=clinic_id)
            .select_related("patient")
            .prefetch_related("offers")
            .order_by("pk")
        )


def patient_waitlist_offers() -> tuple[WaitlistOffer, ...]:
    """Expose only the current enrollment, without installing a staff identity."""
    scope = require_patient_booking_scope()
    with transaction.atomic():
        offers = (
            WaitlistOffer.objects.filter(
                organization_id=scope.organization_id,
                clinic_id=scope.clinic_id,
                entry__enrollment_id=scope.enrollment_id,
            )
            .select_related("entry")
            .order_by("-created_at", "pk")
        )
        for practitioner_id in sorted({offer.practitioner_id for offer in offers}):
            _lock(scope.clinic_id, practitioner_id)
        for offer in offers:
            current = WaitlistOffer.objects.select_for_update().get(pk=offer.pk)
            _expire(current)
            offer.state = current.state
        return tuple(offers)


def respond_to_offer(offer_id: UUID, *, accept: bool) -> WaitlistOffer:
    """Accept atomically through booking, or retain a recoverable terminal result."""
    scope = require_patient_booking_scope()
    with transaction.atomic():
        try:
            offer = WaitlistOffer.objects.get(
                pk=offer_id,
                organization_id=scope.organization_id,
                clinic_id=scope.clinic_id,
                entry__enrollment_id=scope.enrollment_id,
            )
        except WaitlistOffer.DoesNotExist as error:
            raise AppointmentAccessDeniedError from error
        _lock(scope.clinic_id, offer.practitioner_id)
        offer = WaitlistOffer.objects.select_for_update().get(pk=offer.pk)
        if _expire(offer) or offer.state != "pending":
            return offer
        if not accept:
            return _finish(offer, "declined", "declined")
        try:
            # The nested transaction rolls a rejected booking back, not its receipt.
            with transaction.atomic():
                appointment = _book_offer(scope, offer)
        except _ExpiredDuringBookingError:
            return _finish(offer, "expired", "expired")
        except (
            SlotConflict,
            AppointmentAvailabilityError,
            AppointmentPractitionerError,
            AppointmentCreateInputError,
            AppointmentIdempotencyConflictError,
        ):
            return _finish(offer, "unavailable", "waiting")
        offer.appointment = appointment
        return _finish(offer, "accepted", "fulfilled")


def _book_offer(scope: PatientBookingScope, offer: WaitlistOffer) -> Appointment:
    appointment = create_appointment(
        clinic_id=scope.clinic_id,
        enrollment_id=scope.enrollment_id,
        practitioner_id=offer.practitioner_id,
        local_range=AppointmentLocalRange(
            format_local_minute(offer.start_at, scope.timezone),
            format_local_minute(offer.end_at, scope.timezone),
        ),
        idempotency_key=offer.pk,
    )
    if offer.expires_at <= timezone.now():
        raise _ExpiredDuringBookingError
    if appointment.status != Appointment.Status.SCHEDULED or (
        appointment.start_at,
        appointment.end_at,
    ) != (offer.start_at, offer.end_at):
        raise AppointmentAvailabilityError
    return appointment


def waitlist_notice_channels(offer_id: UUID) -> tuple[str, ...]:
    """Recheck explicit offer consent and current verification; never claim sent.

    The portal is authoritative. External delivery belongs to the channel worker;
    it must call this gate immediately before sending, not cache its result.
    """
    offer = WaitlistOffer.objects.select_related("entry").get(pk=offer_id)
    authorized_appointment_manager_clinic(offer.clinic_id)
    if offer.state != "pending" or offer.expires_at <= timezone.now():
        return ()
    contacts = PatientContact.objects.filter(
        organization_id=offer.organization_id,
        patient_id=offer.entry.patient_id,
        destination_version=models.F("verified_version"),
    ).values("channel")
    return tuple(
        PatientChannelPreference.objects.filter(
            organization_id=offer.organization_id,
            clinic_id=offer.clinic_id,
            patient_id=offer.entry.patient_id,
            purpose="waitlist_offer",
            opted_in=True,
            channel__in=contacts,
        )
        .order_by("channel")
        .values_list("channel", flat=True)
    )

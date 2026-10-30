"""Patient portal v2: destinations, summaries, representation and display.

Every destination is keyed to one session operation, and an operation the
session does not hold leaves no trace (never a placeholder). Summaries read
only through the existing patient-session resolvers; a resolver that refuses
between the overview read and the summary read hides that summary, failing
closed. Portal messages (todo 51) and PSP payment links (todo 57) are
delivered by their own todos and join ``DESTINATIONS`` then.

Display preferences live in ``portal_display`` (server-side session only).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Final
from uuid import UUID
from zoneinfo import ZoneInfo

from django.db import connection
from django.utils import timezone
from django.utils.translation import gettext_lazy as _

from apps.billing.presentation import patient_ledger
from apps.core.secrets import secret_store
from apps.intake.access import PatientAccessDeniedError
from apps.intake.questionnaires import patient_responses
from apps.scheduling.access import AppointmentAccessDeniedError
from apps.scheduling.patient_authority import patient_booking_scope
from apps.scheduling.patient_booking import patient_appointments
from apps.tenancy.envelope import KEK_SECRET_NAME

if TYPE_CHECKING:
    from django_stubs_ext import StrOrPromise

UPCOMING_STATUSES: Final = frozenset({"requested", "held", "scheduled"})
LOCAL_MINUTE: Final = "%Y-%m-%dT%H:%M"
# A browser notification would carry only this fixed copy and no record data;
# delivery stays unavailable until a governed push capability exists.
PUSH_NOTICE: Final = _("You have a new message from the clinic.")


@dataclass(frozen=True, slots=True)
class PortalDestination:
    """One portal surface the session's operation set opens."""

    key: str
    operation: str
    url_name: str
    element_id: str
    label: StrOrPromise
    hint: StrOrPromise


DESTINATIONS: Final = (
    PortalDestination(
        "appointments",
        "booking",
        "scheduling:patient-booking",
        "appointments-link",
        _("Your appointments"),
        _("Book, reschedule or cancel a visit with the clinic."),
    ),
    PortalDestination(
        "questionnaires",
        "questionnaires",
        "intake:questionnaires",
        "questionnaires-link",
        _("Forms before your visit"),
        _("Answer at your own pace; saved drafts stay with the clinic."),
    ),
    PortalDestination(
        "teleconsult",
        "teleconsult",
        "teleconsult:patient",
        "teleconsult-link",
        _("Teleconsultation"),
        _("Check camera and microphone, then join when the clinic opens the room."),
    ),
    PortalDestination(
        "results",
        "records",
        "orders:patient-results",
        "results-link",
        _("Your results"),
        _("Exam results your clinician reviewed and released to you."),
    ),
    PortalDestination(
        "documents",
        "records",
        "prescription:patient-documents",
        "documents-link",
        _("Your documents"),
        _("Prescriptions and documents the clinic released to you."),
    ),
    PortalDestination(
        "records",
        "records",
        "retention:patient-records",
        "records-link",
        _("Your records"),
        _("Released record versions and a verifiable copy to download."),
    ),
    PortalDestination(
        "charges",
        "billing",
        "billing:patient-charges",
        "charges-link",
        _("Your charges"),
        _("Amounts and payment instructions the clinic released to you."),
    ),
    PortalDestination(
        "consent",
        "consent",
        "consent:patient",
        "consent-link",
        _("Your consents"),
        _("Read, accept or withdraw each purpose separately."),
    ),
    PortalDestination(
        "preferences",
        "enrollment_view",
        "intake:patient-preferences",
        "preferences-link",
        _("Preferences"),
        _("Text size, contrast and how the clinic contacts you."),
    ),
    PortalDestination(
        "data-requests",
        "enrollment_view",
        "intake:patient-data-requests",
        "data-requests-link",
        _("Your data rights"),
        _("Ask for a copy or a correction, or see who may handle your data."),
    ),
)


@dataclass(frozen=True, slots=True)
class NextAppointment:
    """The earliest upcoming visit, already converted to the clinic minute."""

    local_minute: str
    status: str


@dataclass(frozen=True, slots=True)
class DelegateOption:
    """One verified, active representation the original identity may use."""

    grant_id: UUID
    patient_name: str
    expires_at: datetime
    current: bool


@dataclass(frozen=True, slots=True)
class PortalHome:
    """Everything the v2 home renders beyond the session overview."""

    clinic_id: UUID | None
    destinations: tuple[PortalDestination, ...]
    next_appointment: NextAppointment | None
    pending_forms: int
    open_charges: int
    acting_for_patient: bool
    delegate_options: tuple[DelegateOption, ...]


@dataclass(frozen=True, slots=True)
class ChannelPreference:
    """One messaging purpose and channel the clinic may use, without addresses."""

    purpose: str
    channel: str
    opted_in: bool
    verified: bool


def portal_home(operations: tuple[str, ...]) -> PortalHome:
    """Compose the home strictly from the operations the session holds."""
    granted = frozenset(operations)
    return PortalHome(
        clinic_id=_session_clinic(),
        destinations=tuple(
            destination
            for destination in DESTINATIONS
            if destination.operation in granted
        ),
        next_appointment=_next_appointment() if "booking" in granted else None,
        pending_forms=_pending_forms() if "questionnaires" in granted else 0,
        open_charges=_open_charges() if "billing" in granted else 0,
        acting_for_patient=_acting_for_patient(),
        delegate_options=delegate_options(),
    )


def _session_clinic() -> UUID | None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_id FROM clinic_app.current_patient_authority(NULL)"
        )
        row = cursor.fetchone()
    return row[0] if row is not None and isinstance(row[0], UUID) else None


def _next_appointment() -> NextAppointment | None:
    scope = patient_booking_scope()
    if scope is None:
        return None
    try:
        appointments = patient_appointments()
    except AppointmentAccessDeniedError:
        return None
    now = timezone.now()
    zone = ZoneInfo(scope.timezone)
    for appointment in appointments:
        if appointment.status in UPCOMING_STATUSES and appointment.start_at >= now:
            return NextAppointment(
                local_minute=appointment.start_at.astimezone(zone).strftime(
                    LOCAL_MINUTE
                ),
                status=appointment.status,
            )
    return None


def _pending_forms() -> int:
    try:
        responses = patient_responses()
    except PatientAccessDeniedError:
        return 0
    return sum(1 for response in responses if response.state != "submitted")


def _open_charges() -> int:
    return sum(1 for charge in patient_ledger() if charge.charge.state == "open")


def _acting_for_patient() -> bool:
    with connection.cursor() as cursor:
        cursor.execute("SELECT clinic_app.patient_is_delegate()")
        row = cursor.fetchone()
    return row is not None and row[0] is True


def delegate_options() -> tuple[DelegateOption, ...]:
    """List the active, verified grants the original identity may switch to."""
    kek = secret_store().get_secret(KEK_SECRET_NAME)
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.patient_delegate_options(%s)", [kek])
        rows = cursor.fetchall()
    return tuple(
        DelegateOption(
            grant_id=grant_id, patient_name=name, expires_at=expires_at, current=current
        )
        for grant_id, name, expires_at, current in rows
        if isinstance(grant_id, UUID)
        and isinstance(name, str)
        and isinstance(expires_at, datetime)
        and isinstance(current, bool)
    )


def channel_preferences() -> tuple[ChannelPreference, ...]:
    """Summarize the session subject's messaging channels, never destinations."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.patient_channel_preferences()")
        rows = cursor.fetchall()
    return tuple(
        ChannelPreference(
            purpose=str(purpose),
            channel=str(channel),
            opted_in=opted_in is True,
            verified=verified is True,
        )
        for purpose, channel, opted_in, verified in rows
    )

"""Verified patient contacts and per-channel messaging preferences.

Contacts are organization-level patient destinations; preferences are
clinic-scoped, versioned opt-in states per purpose and channel. A missing
preference row means no permission, so automated sends fail closed and no
processing legal basis is inferred from a messaging choice. Verification
binds to the exact destination version: changing the destination
invalidates it, and verifying one patient's contact never verifies another
patient's identical destination. Destinations render masked everywhere
except the explicit edit screen.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from django.core.exceptions import ValidationError
from django.core.validators import EmailValidator
from django.db import models, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.comms.models import IntegrationOperation
from apps.core.integration import (
    OperationRequest,
    enqueue_operation,
    hold_subject_mutation_key,
    hold_subject_mutation_lock,
)
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_username,
)
from apps.intake.access import (
    PatientAccessDeniedError,
)
from apps.intake.access import (
    authorized_enrollment as _authorized_enrollment,
)
from apps.intake.models import (
    CONTACT_CHANNEL_VALUES,
    CONTACT_PURPOSE_VALUES,
    PatientChannelPreference,
    PatientContact,
    PatientContactEvent,
)

if TYPE_CHECKING:
    from uuid import UUID

    from apps.comms.adapters import OperationScope
    from apps.identity.models import Clinic

SUBJECT_TYPE_PREFERENCE: Final = "intake.patient_channel_preference"
VERIFICATION_METHOD_ATTESTED: Final = "staff_attested"
MAX_DESTINATION_LENGTH: Final = 255
MAX_ACTOR_LABEL_LENGTH: Final = 150
HISTORY_LIMIT: Final = 20
PHONE_PATTERN: Final = re.compile(r"^\+?\d{8,15}$")
PHONE_TAIL: Final = 4
_EMAIL_VALIDATOR: Final = EmailValidator()
_CONTACT_EVENT_TYPES: Final = frozenset(
    {
        PatientContactEvent.EventType.CONTACT_SAVED,
        PatientContactEvent.EventType.CONTACT_VERIFIED,
        PatientContactEvent.EventType.VERIFICATION_INVALIDATED,
    }
)


class ContactInputError(ValueError):
    """Reject malformed contact input without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("contact input is invalid")


class ContactConflictError(Exception):
    """Reject a stale contact write instead of overwriting newer data."""

    def __init__(self) -> None:
        """Expose one stable non-identifying conflict message."""
        super().__init__("contact changed since it was loaded")


class AutomatedMessageError(ValueError):
    """Reject an automated send without a verified, opted-in destination."""

    def __init__(self) -> None:
        """Expose one stable non-identifying refusal message."""
        super().__init__("automated message destination is not eligible")


@dataclass(frozen=True, slots=True)
class ContactView:
    """One contact row with the destination masked for display."""

    channel: str
    masked_destination: str
    verified: bool
    destination_version: int


@dataclass(frozen=True, slots=True)
class PreferenceView:
    """One clinic-scoped opt-in state for a purpose and channel."""

    purpose: str
    channel: str
    opted_in: bool
    version: int


@dataclass(frozen=True, slots=True)
class ContactEventView:
    """One history row: actor, transition and resulting version."""

    event_type: str
    actor_label: str
    channel: str
    purpose: str
    version: int
    created_at: object


@dataclass(frozen=True, slots=True)
class ContactOverview:
    """Everything the manage screen renders for one enrollment."""

    enrollment_id: UUID
    patient_display_name: str
    contacts: tuple[ContactView, ...]
    preferences: tuple[PreferenceView, ...]
    history: tuple[ContactEventView, ...]


@dataclass(frozen=True, slots=True)
class ContactEditTarget:
    """The full destination shown only on the explicit edit screen."""

    enrollment_id: UUID
    patient_display_name: str
    channel: str
    destination: str
    destination_version: int


def mask_destination(channel: str, destination: str) -> str:
    """Mask one destination for every surface except the edit screen."""
    if channel == PatientContact.Channel.EMAIL:
        local, separator, domain = destination.partition("@")
        if not separator or not domain:
            return "•••"
        _, dot, tld = domain.rpartition(".")
        suffix = f".{tld}" if dot and tld else ""
        return f"{local[:1]}•••@{domain[:1]}•••{suffix}"
    digits = "".join(character for character in destination if character.isdigit())
    return f"••• •••• {digits[-PHONE_TAIL:]}" if len(digits) >= PHONE_TAIL else "••••"


def _normalize_destination(channel: str, destination: str) -> str:
    if type(destination) is not str:
        raise ContactInputError
    if channel == PatientContact.Channel.EMAIL:
        normalized = " ".join(destination.split()).lower()
        try:
            _EMAIL_VALIDATOR(normalized)
        except ValidationError as error:
            raise ContactInputError from error
    elif channel in (
        PatientContact.Channel.SMS,
        PatientContact.Channel.WHATSAPP,
    ):
        normalized = re.sub(r"[\s().\-]", "", destination)
        if PHONE_PATTERN.fullmatch(normalized) is None:
            raise ContactInputError
    else:
        raise ContactInputError
    if not 1 <= len(normalized) <= MAX_DESTINATION_LENGTH:
        raise ContactInputError
    return normalized


def _actor_label() -> str:
    """Capture the current actor's username through the trusted resolver."""
    try:
        return current_actor_username()[:MAX_ACTOR_LABEL_LENGTH]
    except CurrentActorError as error:
        raise PatientAccessDeniedError from error


def _patient_channel_lock_key(patient_id: UUID, channel: str) -> str:
    """Return the stable mutation-boundary key for one patient channel.

    Preference creation, destination invalidation and the send boundary
    all serialize on this key, so a preference created while a
    destination change is in flight cannot escape the invalidation's
    lock set: the key names the patient channel, not the preference row.
    """
    return f"{SUBJECT_TYPE_PREFERENCE}:{patient_id}:{channel}"


def _record_event(  # noqa: PLR0913 - one event needs its full context
    *,
    clinic: Clinic,
    patient_id: UUID,
    actor_id: UUID,
    actor_label: str,
    event_type: str,
    version: int,
    contact: PatientContact | None = None,
    preference: PatientChannelPreference | None = None,
) -> None:
    PatientContactEvent.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic.pk,
        patient_id=patient_id,
        actor_id=actor_id,
        actor_label=actor_label,
        event_type=event_type,
        contact=contact,
        preference=preference,
        version=version,
    )


def _contact_views(
    organization_id: UUID,
    patient_id: UUID,
) -> tuple[ContactView, ...]:
    contacts = PatientContact.objects.filter(
        organization_id=organization_id,
        patient_id=patient_id,
    ).order_by("channel")
    return tuple(
        ContactView(
            channel=contact.channel,
            masked_destination=mask_destination(contact.channel, contact.destination),
            verified=contact.is_verified,
            destination_version=contact.destination_version,
        )
        for contact in contacts
    )


def _preference_views(
    organization_id: UUID,
    clinic_id: UUID,
    patient_id: UUID,
) -> tuple[PreferenceView, ...]:
    preferences = PatientChannelPreference.objects.filter(
        organization_id=organization_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
    ).order_by("purpose", "channel")
    return tuple(
        PreferenceView(
            purpose=preference.purpose,
            channel=preference.channel,
            opted_in=preference.opted_in,
            version=preference.version,
        )
        for preference in preferences
    )


def _history_views(
    organization_id: UUID,
    clinic_id: UUID,
    patient_id: UUID,
) -> tuple[ContactEventView, ...]:
    events = (
        PatientContactEvent.objects.select_related("contact", "preference")
        .filter(
            organization_id=organization_id,
            clinic_id=clinic_id,
            patient_id=patient_id,
        )
        .order_by("-created_at", "event_type")[:HISTORY_LIMIT]
    )
    views: list[ContactEventView] = []
    for event in events:
        if event.event_type in _CONTACT_EVENT_TYPES:
            channel = event.contact.channel if event.contact else ""
            purpose = ""
        else:
            channel = event.preference.channel if event.preference else ""
            purpose = event.preference.purpose if event.preference else ""
        views.append(
            ContactEventView(
                event_type=event.event_type,
                actor_label=event.actor_label,
                channel=channel,
                purpose=purpose,
                version=event.version,
                created_at=event.created_at,
            )
        )
    return tuple(views)


def contact_overview(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
) -> ContactOverview:
    """Return the masked manage-screen state for one enrolled patient."""
    with transaction.atomic():
        _, clinic, enrollment = _authorized_enrollment(clinic_id, enrollment_id)
        overview = ContactOverview(
            enrollment_id=enrollment.pk,
            patient_display_name=enrollment.patient.full_name,
            contacts=_contact_views(clinic.organization_id, enrollment.patient_id),
            preferences=_preference_views(
                clinic.organization_id, clinic.pk, enrollment.patient_id
            ),
            history=_history_views(
                clinic.organization_id, clinic.pk, enrollment.patient_id
            ),
        )
        record_phase1_event(
            "intake.contacts.viewed",
            clinic_id=clinic_id,
            affected_record_id=enrollment.pk,
        )
        return overview


def contact_for_edit(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    channel: str,
) -> ContactEditTarget:
    """Return the full destination for the explicit edit screen only."""
    if channel not in CONTACT_CHANNEL_VALUES:
        raise ContactInputError
    with transaction.atomic():
        _, clinic, enrollment = _authorized_enrollment(clinic_id, enrollment_id)
        contact = PatientContact.objects.filter(
            organization_id=clinic.organization_id,
            patient_id=enrollment.patient_id,
            channel=channel,
        ).first()
        return ContactEditTarget(
            enrollment_id=enrollment.pk,
            patient_display_name=enrollment.patient.full_name,
            channel=channel,
            destination=contact.destination if contact else "",
            destination_version=contact.destination_version if contact else 0,
        )


def save_contact_destination(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    channel: str,
    destination: str,
    expected_version: int | None,
) -> PatientContact:
    """Create or change one destination; a change invalidates verification.

    ``expected_version`` is the version the edit screen rendered: ``None``
    means no contact existed, so a concurrent create or change is a
    conflict instead of a silent overwrite.
    """
    if channel not in CONTACT_CHANNEL_VALUES:
        raise ContactInputError
    normalized = _normalize_destination(channel, destination)
    if expected_version is not None and (
        type(expected_version) is not int or expected_version < 1
    ):
        raise ContactInputError
    with transaction.atomic():
        actor_id, clinic, enrollment = _authorized_enrollment(clinic_id, enrollment_id)
        actor_label = _actor_label()
        contact = (
            PatientContact.objects.select_for_update()
            .filter(
                organization_id=clinic.organization_id,
                patient_id=enrollment.patient_id,
                channel=channel,
            )
            .first()
        )
        if contact is None:
            if expected_version is not None:
                raise ContactConflictError
            contact = PatientContact.objects.create(
                organization_id=clinic.organization_id,
                patient_id=enrollment.patient_id,
                channel=channel,
                destination=normalized,
            )
            _record_event(
                clinic=clinic,
                patient_id=enrollment.patient_id,
                actor_id=actor_id,
                actor_label=actor_label,
                event_type=PatientContactEvent.EventType.CONTACT_SAVED,
                contact=contact,
                version=contact.destination_version,
            )
            record_phase1_event(
                "intake.contact.saved",
                clinic_id=clinic_id,
                affected_record_id=contact.pk,
            )
        else:
            if expected_version != contact.destination_version:
                raise ContactConflictError
            if normalized != contact.destination:
                was_verified = contact.is_verified
                # Serialize with any send boundary rechecking or
                # delivering for a preference on this channel: the
                # invalidation commits before the send's final recheck
                # or after the provider answered.
                for preference_id in (
                    PatientChannelPreference.objects.filter(
                        organization_id=clinic.organization_id,
                        patient_id=enrollment.patient_id,
                        channel=channel,
                    )
                    .order_by("pk")
                    .values_list("pk", flat=True)
                ):
                    hold_subject_mutation_lock(SUBJECT_TYPE_PREFERENCE, preference_id)
                contact.destination = normalized
                contact.destination_version += 1
                contact.verified_version = 0
                contact.verified_at = None
                contact.verification_method = ""
                contact.save(
                    update_fields=(
                        "destination",
                        "destination_version",
                        "verified_version",
                        "verified_at",
                        "verification_method",
                        "updated_at",
                    )
                )
                # The stable patient-channel boundary lock covers
                # preferences created after the enumeration above: a
                # concurrent create holds the same key until it
                # commits, and a send re-decides under it before the
                # external call.
                hold_subject_mutation_key(
                    _patient_channel_lock_key(enrollment.patient_id, channel)
                )
                _record_event(
                    clinic=clinic,
                    patient_id=enrollment.patient_id,
                    actor_id=actor_id,
                    actor_label=actor_label,
                    event_type=PatientContactEvent.EventType.CONTACT_SAVED,
                    contact=contact,
                    version=contact.destination_version,
                )
                if was_verified:
                    _record_event(
                        clinic=clinic,
                        patient_id=enrollment.patient_id,
                        actor_id=actor_id,
                        actor_label=actor_label,
                        event_type=(
                            PatientContactEvent.EventType.VERIFICATION_INVALIDATED
                        ),
                        contact=contact,
                        version=contact.destination_version,
                    )
                record_phase1_event(
                    "intake.contact.saved",
                    clinic_id=clinic_id,
                    affected_record_id=contact.pk,
                )
        return contact


def verify_contact(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    channel: str,
    expected_version: int,
) -> PatientContact:
    """Record staff attestation that the rendered destination was verified.

    ``expected_version`` is the destination version the manage screen
    rendered: a destination changed since then is a conflict, never a
    silent verification of an unseen value. The synthetic method is
    explicit in the record; verification binds to this patient's
    contact row and destination version only.
    """
    if channel not in CONTACT_CHANNEL_VALUES:
        raise ContactInputError
    if type(expected_version) is not int or expected_version < 1:
        raise ContactInputError
    with transaction.atomic():
        actor_id, clinic, enrollment = _authorized_enrollment(clinic_id, enrollment_id)
        actor_label = _actor_label()
        contact = (
            PatientContact.objects.select_for_update()
            .filter(
                organization_id=clinic.organization_id,
                patient_id=enrollment.patient_id,
                channel=channel,
            )
            .first()
        )
        if contact is None:
            raise ContactInputError
        if expected_version != contact.destination_version:
            raise ContactConflictError
        if not contact.is_verified:
            contact.verified_version = contact.destination_version
            contact.verified_at = timezone.now()
            contact.verification_method = VERIFICATION_METHOD_ATTESTED
            contact.save(
                update_fields=(
                    "verified_version",
                    "verified_at",
                    "verification_method",
                    "updated_at",
                )
            )
            _record_event(
                clinic=clinic,
                patient_id=enrollment.patient_id,
                actor_id=actor_id,
                actor_label=actor_label,
                event_type=PatientContactEvent.EventType.CONTACT_VERIFIED,
                contact=contact,
                version=contact.verified_version,
            )
            record_phase1_event(
                "intake.contact.verified",
                clinic_id=clinic_id,
                affected_record_id=contact.pk,
            )
        return contact


def _set_preference(  # noqa: PLR0913 - one transition needs its full context
    *,
    clinic: Clinic,
    patient_id: UUID,
    actor_id: UUID,
    actor_label: str,
    purpose: str,
    channel: str,
    opted_in: bool,
) -> PatientChannelPreference | None:
    """Transition one preference row; return it only when it changed."""
    preference = (
        PatientChannelPreference.objects.select_for_update()
        .filter(
            organization_id=clinic.organization_id,
            clinic_id=clinic.pk,
            patient_id=patient_id,
            purpose=purpose,
            channel=channel,
        )
        .first()
    )
    if preference is None:
        if not opted_in:
            return None
        preference = PatientChannelPreference.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic.pk,
            patient_id=patient_id,
            purpose=purpose,
            channel=channel,
            opted_in=True,
        )
    else:
        if preference.opted_in == opted_in:
            return None
        preference.opted_in = opted_in
        preference.version += 1
        preference.save(update_fields=("opted_in", "version", "updated_at"))
    # Serialize with the send boundary: a committed opt-in change either
    # lands before the send's final recheck or after the provider call.
    # The stable patient-channel key also orders creation against a
    # destination invalidation that enumerated preferences before this
    # row existed.
    hold_subject_mutation_lock(SUBJECT_TYPE_PREFERENCE, preference.pk)
    hold_subject_mutation_key(_patient_channel_lock_key(patient_id, channel))
    _record_event(
        clinic=clinic,
        patient_id=patient_id,
        actor_id=actor_id,
        actor_label=actor_label,
        event_type=(
            PatientContactEvent.EventType.PREFERENCE_OPTED_IN
            if opted_in
            else PatientContactEvent.EventType.PREFERENCE_OPTED_OUT
        ),
        preference=preference,
        version=preference.version,
    )
    record_phase1_event(
        "intake.preference.opted_in" if opted_in else "intake.preference.opted_out",
        clinic_id=clinic.pk,
        affected_record_id=preference.pk,
    )
    return preference


def set_purpose_channel(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    purpose: str,
    channel: str | None,
) -> tuple[PatientChannelPreference, ...]:
    """Choose one channel for a purpose, or revoke it with ``None``.

    Choosing a channel opts that channel in and every other channel out;
    ``None`` revokes the purpose on all channels. Every transition bumps
    the preference version and appends history.
    """
    if purpose not in CONTACT_PURPOSE_VALUES:
        raise ContactInputError
    if channel is not None and channel not in CONTACT_CHANNEL_VALUES:
        raise ContactInputError
    with transaction.atomic():
        actor_id, clinic, enrollment = _authorized_enrollment(clinic_id, enrollment_id)
        actor_label = _actor_label()
        changed: list[PatientChannelPreference] = []
        for candidate in CONTACT_CHANNEL_VALUES:
            preference = _set_preference(
                clinic=clinic,
                patient_id=enrollment.patient_id,
                actor_id=actor_id,
                actor_label=actor_label,
                purpose=purpose,
                channel=candidate,
                opted_in=candidate == channel,
            )
            if preference is not None:
                changed.append(preference)
        return tuple(changed)


def _eligible_preference(
    organization_id: UUID,
    clinic_id: UUID,
    subject_id: UUID,
) -> PatientChannelPreference | None:
    return (
        PatientChannelPreference.objects.filter(
            organization_id=organization_id,
            clinic_id=clinic_id,
            pk=subject_id,
            opted_in=True,
        )
        .select_related(None)
        .first()
    )


def _verified_contact(
    organization_id: UUID,
    patient_id: UUID,
    channel: str,
) -> PatientContact | None:
    return PatientContact.objects.filter(
        organization_id=organization_id,
        patient_id=patient_id,
        channel=channel,
        destination_version=models.F("verified_version"),
    ).first()


def preference_lock_key(scope: OperationScope, subject_id: UUID) -> str | None:
    """Resolve the send boundary's stable lock key for one preference.

    The key names the patient channel, not the preference row, so sends
    serialize with preference creation and destination invalidation on
    that channel even when the row did not exist while the mutation
    enumerated preferences.
    """
    preference = (
        PatientChannelPreference.objects.filter(
            organization_id=scope.organization_id,
            pk=subject_id,
        )
        .select_related(None)
        .first()
    )
    if preference is None:
        return None
    return _patient_channel_lock_key(preference.patient_id, preference.channel)


def preference_send_eligible(scope: OperationScope) -> bool:
    """Recheck one preference subject at send time inside tenant context.

    The stored operation's clinic must match the preference's clinic, the
    preference must still be opted in for the operation's channel, and a
    contact for that channel must still be verified at its current
    destination version. Anything else cancels without an external send.
    """
    operation = IntegrationOperation.objects.filter(pk=scope.operation_id).first()
    if operation is None or operation.clinic_id != scope.clinic_id:
        return False
    preference = _eligible_preference(
        scope.organization_id, scope.clinic_id, operation.subject_id
    )
    if preference is None or preference.channel != operation.channel:
        return False
    return (
        _verified_contact(
            scope.organization_id, preference.patient_id, preference.channel
        )
        is not None
    )


def enqueue_automated_message(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    purpose: str,
    provider: str,
    idempotency_key: UUID,
) -> UUID:
    """Queue one automated message for the patient's chosen channel.

    The current preference and verification state are checked now; the
    send-time boundary rechecks both again before any external call.
    """
    if purpose not in CONTACT_PURPOSE_VALUES:
        raise AutomatedMessageError
    with transaction.atomic():
        _, clinic, enrollment = _authorized_enrollment(clinic_id, enrollment_id)
        preference = (
            PatientChannelPreference.objects.filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                patient_id=enrollment.patient_id,
                purpose=purpose,
                opted_in=True,
            )
            .order_by("channel")
            .first()
        )
        if preference is None:
            raise AutomatedMessageError
        if (
            _verified_contact(
                clinic.organization_id,
                enrollment.patient_id,
                preference.channel,
            )
            is None
        ):
            raise AutomatedMessageError
        return enqueue_operation(
            OperationRequest(
                channel=preference.channel,
                provider=provider,
                clinic_id=clinic.pk,
                subject_type=SUBJECT_TYPE_PREFERENCE,
                subject_id=preference.pk,
                idempotency_key=idempotency_key,
            )
        )

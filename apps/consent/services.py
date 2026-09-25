"""Exact consent receipts with explicit patient authority and retained refusal."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import TYPE_CHECKING
from uuid import UUID

from django.core import signing
from django.core.exceptions import ValidationError
from django.db import connection, transaction
from django.utils import timezone

from apps.audit.events import build_phase1_audit_event
from apps.audit.services import AuditTrustedContext, _content_hash, record_phase1_event
from apps.consent.models import (
    AIUseDisclosure,
    ConsentAcceptance,
    ConsentPurpose,
    ConsentRevocation,
    ConsentText,
    NoticeTopic,
    NoticeVersion,
    ParticipantAcknowledgment,
    ParticipantKind,
    RefusalRecord,
)
from apps.ehr.models import Encounter
from apps.identity.current_context import require_current_actor_clinic_roles
from apps.identity.models import Clinic, UserClinicRole
from apps.identity.overlay_content import validate_overlay_text
from apps.intake.access import PatientAccessDeniedError

if TYPE_CHECKING:
    from django.db.models import QuerySet

MAX_TEXT = 20000
OFFER_MAX_AGE = 1800
STAFF_ROLES = tuple(UserClinicRole.Role)
READ_ROLES = (
    UserClinicRole.Role.OWNER,
    UserClinicRole.Role.CLINIC_ADMIN,
    UserClinicRole.Role.RECEPTIONIST,
    UserClinicRole.Role.PHYSICIAN,
)
CLINICIAN_ROLES = (
    UserClinicRole.Role.PHYSICIAN,
    UserClinicRole.Role.NURSE,
    UserClinicRole.Role.ALLIED_PROFESSIONAL,
)


@dataclass(frozen=True)
class ConsentAuthority:
    """Only server-resolved patient authority; no caller supplies these values."""

    session_id: UUID
    organization_id: UUID
    clinic_id: UUID
    patient_id: UUID
    enrollment_id: UUID


def patient_authority() -> ConsentAuthority:
    """Revalidate the live consent operation without trusting session-shaped input."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT * FROM clinic_app.consent_session()")
        row = cursor.fetchone()
    if row is None:
        raise PatientAccessDeniedError
    return ConsentAuthority(*row)


def _lock(clinic_id: UUID, purpose: str) -> None:
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT pg_advisory_xact_lock(hashtextextended(%s,0))",
            [f"consent:{clinic_id}:{purpose}"],
        )


def publish_text(
    *, clinic_id: UUID, purpose: str, text: str, language: str = "pt-BR"
) -> ConsentText:
    """Publish an authorized clinic overlay as a new immutable version."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
    )
    if (
        purpose not in ConsentPurpose.values
        or language != "pt-BR"
        or not text.strip()
        or len(text) > MAX_TEXT
    ):
        msg = "Texto, finalidade ou idioma inválido."
        raise ValidationError(msg)
    validate_overlay_text(text)
    with transaction.atomic():
        _lock(clinic_id, purpose)
        organization_id = Clinic.objects.values_list("organization_id", flat=True).get(
            pk=clinic_id
        )
        previous = (
            ConsentText.objects.filter(clinic_id=clinic_id, purpose=purpose)
            .order_by("-version")
            .first()
        )
        result = ConsentText.objects.create(
            organization_id=organization_id,
            clinic_id=clinic_id,
            purpose=purpose,
            version=previous.version + 1 if previous else 1,
            text=text,
            language=language,
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            published_by_id=actor,
        )
        record_phase1_event(
            "consent.text.published", clinic_id=clinic_id, affected_record_id=result.pk
        )
        return result


def publish_notice(
    *, clinic_id: UUID, topic: str, text: str, language: str = "pt-BR"
) -> NoticeVersion:
    """Publish an information-only notice version; it never authorizes use."""
    actor = require_current_actor_clinic_roles(
        clinic_id, (UserClinicRole.Role.OWNER, UserClinicRole.Role.CLINIC_ADMIN)
    )
    if (
        topic not in NoticeTopic.values
        or language != "pt-BR"
        or not text.strip()
        or len(text) > MAX_TEXT
    ):
        msg = "Texto, finalidade ou idioma inválido."
        raise ValidationError(msg)
    validate_overlay_text(text)
    with transaction.atomic():
        _lock(clinic_id, f"notice:{topic}")
        organization_id = Clinic.objects.values_list("organization_id", flat=True).get(
            pk=clinic_id
        )
        previous = (
            NoticeVersion.objects.filter(clinic_id=clinic_id, topic=topic)
            .order_by("-version")
            .first()
        )
        result = NoticeVersion.objects.create(
            organization_id=organization_id,
            clinic_id=clinic_id,
            topic=topic,
            version=previous.version + 1 if previous else 1,
            text=text,
            language=language,
            digest=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            published_by_id=actor,
        )
        record_phase1_event(
            "consent.notice.published",
            clinic_id=clinic_id,
            affected_record_id=result.pk,
        )
        return result


def available_texts() -> list[ConsentText]:
    """Return the latest available purpose-specific text for this patient clinic."""
    authority = patient_authority()
    return list(
        ConsentText.objects.filter(clinic_id=authority.clinic_id)
        .order_by("purpose", "-version")
        .distinct("purpose")
    )


def available_notices() -> list[NoticeVersion]:
    """Return the latest information-only notices for this patient clinic."""
    authority = patient_authority()
    return list(
        NoticeVersion.objects.filter(clinic_id=authority.clinic_id)
        .order_by("topic", "-version")
        .distinct("topic")
    )


def prepare_acceptance(*, text_id: UUID) -> tuple[ConsentText, str]:
    """Bind the displayed text to this session; POST-body tokens never go in URLs."""
    authority = patient_authority()
    text = next((t for t in available_texts() if t.pk == text_id), None)
    if text is None:
        raise PatientAccessDeniedError
    token = signing.dumps(
        {
            "text": str(text.pk),
            "purpose": text.purpose,
            "digest": text.digest,
        },
        salt=f"consent.offer:{authority.session_id}",
    )
    return text, token


def _patient_audit(
    event_name: str, record_id: UUID, authority: ConsentAuthority
) -> None:
    event = build_phase1_audit_event(
        event_name, clinic_id=authority.clinic_id, affected_record_id=record_id
    )
    content_hash = _content_hash(
        event.event,
        AuditTrustedContext(
            organization_id=authority.organization_id,
            actor_user_id=authority.session_id,
        ),
        dict(event.payload),
    )
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT clinic_app.consent_audit(%s,%s,%s,%s)",
            [str(record_id), event_name, event.event.occurred_at_utc, content_hash],
        )


def _resolve_offer(
    offer: str, purpose: str, authority: ConsentAuthority
) -> ConsentText:
    """Resolve an offer token to the current text or deny the action."""
    try:
        data = signing.loads(
            offer, salt=f"consent.offer:{authority.session_id}", max_age=OFFER_MAX_AGE
        )
        if not isinstance(data, dict) or data.get("purpose") != purpose:
            raise PatientAccessDeniedError
        text_id = UUID(data["text"])
    except (signing.BadSignature, ValueError, KeyError, TypeError) as error:
        raise PatientAccessDeniedError from error
    _lock(authority.clinic_id, purpose)
    current = (
        ConsentText.objects.filter(clinic_id=authority.clinic_id, purpose=purpose)
        .order_by("-version")
        .first()
    )
    if current is None or current.pk != text_id or current.digest != data.get("digest"):
        msg = "O texto mudou. Leia a versão atual antes de consentir."
        raise ValidationError(msg)
    return current


def record_consent(*, offer: str, purpose: str, accepted: bool) -> ConsentAcceptance:
    """Accept only the text actually offered to this patient session and purpose."""
    authority = patient_authority()
    if accepted is not True:
        msg = "Marque a opção somente se desejar consentir."
        raise ValidationError(msg)
    with transaction.atomic():
        current = _resolve_offer(offer, purpose, authority)
        result, created = ConsentAcceptance.objects.get_or_create(
            enrollment_id=authority.enrollment_id,
            text=current,
            defaults={
                "organization_id": authority.organization_id,
                "clinic_id": authority.clinic_id,
                "patient_id": authority.patient_id,
                "patient_session_id": authority.session_id,
                "accepted_at": timezone.now(),
            },
        )
        if ConsentRevocation.objects.filter(acceptance=result).exists():
            msg = "Este consentimento foi revogado. O comprovante permanece disponível."
            raise ValidationError(msg)
        if created:
            _patient_audit("consent.accepted", result.pk, authority)
        result.refresh_from_db()
        return result


def record_refusal(*, offer: str, purpose: str) -> RefusalRecord:
    """Retain a deliberate refusal bound to the exact displayed version."""
    authority = patient_authority()
    with transaction.atomic():
        current = _resolve_offer(offer, purpose, authority)
        if ConsentAcceptance.objects.filter(
            enrollment_id=authority.enrollment_id,
            text=current,
            revocation__isnull=True,
        ).exists():
            msg = (
                "Este consentimento está aceito. Revogue-o em vez de registrar "
                "uma recusa."
            )
            raise ValidationError(msg)
        result, created = RefusalRecord.objects.get_or_create(
            enrollment_id=authority.enrollment_id,
            text=current,
            defaults={
                "organization_id": authority.organization_id,
                "clinic_id": authority.clinic_id,
                "patient_id": authority.patient_id,
                "patient_session_id": authority.session_id,
                "refused_at": timezone.now(),
            },
        )
        if created:
            _patient_audit("consent.refused", result.pk, authority)
        result.refresh_from_db()
        return result


def patient_receipts() -> list[ConsentAcceptance]:
    """Read all retained receipts, including superseded and revoked decisions."""
    authority = patient_authority()
    return list(_receipts().filter(enrollment_id=authority.enrollment_id))


def patient_refusals() -> list[RefusalRecord]:
    """Read all retained refusal records for this patient enrollment."""
    authority = patient_authority()
    return list(
        RefusalRecord.objects.select_related("text")
        .filter(enrollment_id=authority.enrollment_id)
        .order_by("-refused_at", "pk")
    )


def _receipts() -> QuerySet[ConsentAcceptance]:
    return ConsentAcceptance.objects.select_related("text", "revocation").order_by(
        "-accepted_at", "pk"
    )


def revoke_consent(*, acceptance_id: UUID) -> ConsentRevocation:
    """Retain the receipt and append one idempotent revocation; touch no EHR rows."""
    authority = patient_authority()
    with transaction.atomic():
        acceptance = (
            ConsentAcceptance.objects.select_related("text")
            .filter(pk=acceptance_id, enrollment_id=authority.enrollment_id)
            .first()
        )
        if acceptance is None:
            raise PatientAccessDeniedError
        _lock(authority.clinic_id, acceptance.text.purpose)
        result, created = ConsentRevocation.objects.get_or_create(
            acceptance=acceptance,
            defaults={
                "organization_id": authority.organization_id,
                "clinic_id": authority.clinic_id,
                "patient_session_id": authority.session_id,
                "revoked_at": timezone.now(),
            },
        )
        if created:
            _patient_audit("consent.revoked", result.pk, authority)
        result.refresh_from_db()
        return result


def staff_receipts(*, clinic_id: UUID, enrollment_id: UUID) -> list[ConsentAcceptance]:
    """Staff may inspect consent provenance, never act on the patient's behalf."""
    require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
    results = list(_receipts().filter(clinic_id=clinic_id, enrollment_id=enrollment_id))
    record_phase1_event(
        "consent.receipts.viewed", clinic_id=clinic_id, affected_record_id=enrollment_id
    )
    return results


def staff_refusals(*, clinic_id: UUID, enrollment_id: UUID) -> list[RefusalRecord]:
    """Staff may inspect retained refusals for one enrollment."""
    require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
    return list(
        RefusalRecord.objects.select_related("text")
        .filter(clinic_id=clinic_id, enrollment_id=enrollment_id)
        .order_by("-refused_at", "pk")
    )


def consent_for_future_use(
    *, clinic_id: UUID, enrollment_id: UUID, purpose: str
) -> ConsentAcceptance | None:
    """Return exact current authority for a future purpose use, or fail closed.

    Consumers must recheck at the point of use, not cache this decision. This
    gate never governs care records, releases or message preferences.
    """
    require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
    if purpose not in ConsentPurpose.values:
        msg = "Finalidade fora da taxonomia de consentimento."
        raise ValidationError(msg)
    current = (
        ConsentText.objects.filter(clinic_id=clinic_id, purpose=purpose)
        .order_by("-version")
        .first()
    )
    if current is None:
        return None
    return ConsentAcceptance.objects.filter(
        clinic_id=clinic_id,
        enrollment_id=enrollment_id,
        text=current,
        revocation__isnull=True,
    ).first()


def record_ai_disclosure(
    *, clinic_id: UUID, encounter_id: UUID, informed: bool, refused: bool
) -> AIUseDisclosure:
    """Record the clinician's per-encounter AI-use attestation.

    A refusal is preserved alongside the attestation and never blocks care.
    """
    actor = require_current_actor_clinic_roles(clinic_id, CLINICIAN_ROLES)
    if (
        type(informed) is not bool
        or type(refused) is not bool
        or (refused and not informed)
    ):
        msg = "Divulgação de uso de IA inválida."
        raise ValidationError(msg)
    encounter = Encounter.objects.filter(clinic_id=clinic_id, pk=encounter_id).first()
    if encounter is None:
        raise PatientAccessDeniedError
    with transaction.atomic():
        result, created = AIUseDisclosure.objects.get_or_create(
            encounter=encounter,
            defaults={
                "organization_id": encounter.organization_id,
                "clinic_id": clinic_id,
                "patient_id": encounter.patient_id,
                "informed": informed,
                "refused": refused,
                "recorded_by_id": actor,
                "recorded_at": timezone.now(),
            },
        )
        if not created and (result.informed != informed or result.refused != refused):
            msg = "Já existe uma divulgação registrada para este atendimento."
            raise ValidationError(msg)
        if created:
            record_phase1_event(
                "consent.ai_disclosure.recorded",
                clinic_id=clinic_id,
                affected_record_id=result.pk,
            )
        result.refresh_from_db()
        return result


def ai_disclosure_status(
    *, clinic_id: UUID, encounter_id: UUID
) -> AIUseDisclosure | None:
    """Return the recorded disclosure for one encounter, or ``None``.

    Consumers (todos 40/44/46) must treat ``None``, ``informed=False`` and
    ``refused=True`` as a refusal of AI assistance for that encounter.
    """
    require_current_actor_clinic_roles(clinic_id, STAFF_ROLES)
    return AIUseDisclosure.objects.filter(
        clinic_id=clinic_id, encounter_id=encounter_id
    ).first()


def acknowledge_participant(
    *, clinic_id: UUID, session_id: UUID, participant_kind: str
) -> ParticipantAcknowledgment:
    """Record that the recording notice reached one non-patient voice."""
    actor = require_current_actor_clinic_roles(clinic_id, CLINICIAN_ROLES)
    if participant_kind not in ParticipantKind.values:
        msg = "Tipo de participante inválido."
        raise ValidationError(msg)
    encounter = Encounter.objects.filter(clinic_id=clinic_id, pk=session_id).first()
    if encounter is None:
        raise PatientAccessDeniedError
    result, _ = ParticipantAcknowledgment.objects.get_or_create(
        session=encounter,
        participant_kind=participant_kind,
        defaults={
            "organization_id": encounter.organization_id,
            "clinic_id": clinic_id,
            "acknowledged_by_clinician_id": actor,
            "acknowledged_at": timezone.now(),
        },
    )
    result.refresh_from_db()
    return result

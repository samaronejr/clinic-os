"""Deterministic same-clinic hints selected solely through tenant HMAC indexes."""

from __future__ import annotations

from collections import defaultdict
from typing import TYPE_CHECKING, Final

from django.db import transaction
from django.db.models import Exists, OuterRef, QuerySet

from apps.audit.services import record_phase1_event
from apps.identity.current_context import CurrentActorError, require_permission
from apps.intake.access import PatientAccessDeniedError
from apps.intake.models import (
    DuplicateCandidate,
    Patient,
    PatientClinicEnrollment,
    PatientContact,
    PatientDuplicateIndex,
    PatientIdentifier,
)
from apps.intake.patient_name_index import normalized_name_bytes
from apps.scheduling.locks import acquire_advisory_locks, patient_lock_key
from apps.tenancy.envelope import blind_indexes_with_versions

if TYPE_CHECKING:
    from uuid import UUID

NAME_PREFIX_LENGTH: Final = 6
NAME_BIRTH_PURPOSE: Final = "intake.duplicate.name_birth.v1"
CONTACT_PURPOSE: Final = "intake.duplicate.contact.v1"
IDENTIFIER_PURPOSE: Final = "intake.patientidentifier.value"


def _current_identifiers() -> QuerySet[PatientIdentifier]:
    newer = PatientIdentifier.objects.filter(
        patient_id=OuterRef("patient_id"),
        kind=OuterRef("kind"),
        version__gt=OuterRef("version"),
    )
    return PatientIdentifier.objects.filter(~Exists(newer), retired_at__isnull=True)


def _current_indexes() -> QuerySet[PatientDuplicateIndex]:
    newer = PatientDuplicateIndex.objects.filter(
        patient_id=OuterRef("patient_id"), version__gt=OuterRef("version")
    )
    return PatientDuplicateIndex.objects.filter(~Exists(newer))


def refresh_duplicate_candidates(*, clinic_id: UUID, patient_id: UUID) -> None:
    """Append current blind indexes and retain review hints, never merge data.

    The calling identity/contact service already holds its write authority.
    All predicates below compare metadata or purpose-bound HMACs, never
    encrypted names, dates, identifiers or contact destinations.
    """
    enrollment = PatientClinicEnrollment.objects.get(
        clinic_id=clinic_id, patient_id=patient_id
    )
    acquire_advisory_locks((patient_lock_key(enrollment.organization_id, patient_id),))
    patient = Patient.objects.get(pk=patient_id)
    name_digests = (
        blind_indexes_with_versions(
            purpose=NAME_BIRTH_PURPOSE,
            plaintext=(
                patient.birth_date.isoformat().encode("ascii")
                + b":"
                + normalized_name_bytes(patient.full_name)
                .decode("utf-8")[:NAME_PREFIX_LENGTH]
                .encode("utf-8")
            ),
        )
        if patient.birth_date is not None
        else ()
    )
    contacts = tuple(PatientContact.objects.filter(patient_id=patient_id))
    contact_digests = tuple(
        blind_indexes_with_versions(
            purpose=CONTACT_PURPOSE,
            plaintext=(
                ("phone" if contact.channel in ("sms", "whatsapp") else contact.channel)
                + f":{contact.destination}"
            ).encode(),
        )
        for contact in contacts
    )
    key_versions = blind_indexes_with_versions(
        purpose=CONTACT_PURPOSE, plaintext=b"version"
    )
    previous = (
        PatientDuplicateIndex.objects.filter(patient_id=patient_id)
        .order_by("-version")
        .first()
    )
    PatientDuplicateIndex.objects.create(
        organization_id=enrollment.organization_id,
        clinic_id=clinic_id,
        patient_id=patient_id,
        version=previous.version + 1 if previous else 1,
        name_birth_index=name_digests[-1].blind_index if name_digests else None,
        contact_indexes=[digests[-1].blind_index for digests in contact_digests],
        index_key_version=key_versions[-1].key_version,
    )
    signals: dict[UUID, set[str]] = defaultdict(set)
    possible = _current_indexes().exclude(patient_id=patient_id)
    if name_digests:
        for match in possible.filter(
            name_birth_index__in=[item.blind_index for item in name_digests]
        ):
            signals[match.patient_id].add("dob+name_prefix")
    if contact_digests:
        for match in possible.filter(
            contact_indexes__overlap=[
                item.blind_index for digests in contact_digests for item in digests
            ]
        ):
            signals[match.patient_id].add("contact")
    for identifier in _current_identifiers().filter(patient_id=patient_id):
        digests = blind_indexes_with_versions(
            purpose=IDENTIFIER_PURPOSE,
            plaintext=f"{identifier.kind}:{identifier.value}".encode(),
        )
        for other in (
            _current_identifiers()
            .filter(
                kind=identifier.kind,
                blind_index__in=[d.blind_index for d in digests],
            )
            .exclude(patient_id=patient_id)
        ):
            signals[other.patient_id].add("exact_identifier")
    for other_enrollment in PatientClinicEnrollment.objects.filter(
        clinic_id=clinic_id, patient_id__in=signals
    ).order_by("id"):
        first, second = sorted((enrollment.pk, other_enrollment.pk))
        candidate, created = DuplicateCandidate.objects.get_or_create(
            organization_id=enrollment.organization_id,
            clinic_id=clinic_id,
            enrollment_a_id=first,
            enrollment_b_id=second,
            defaults={"signals": sorted(signals[other_enrollment.patient_id])},
        )
        if created:
            record_phase1_event(
                "intake.duplicate.detected",
                clinic_id=clinic_id,
                affected_record_id=candidate.pk,
            )


def duplicate_candidates(*, clinic_id: UUID) -> tuple[DuplicateCandidate, ...]:
    """Expose same-clinic metadata hints without granting adjudication."""
    with transaction.atomic():
        try:
            require_permission("demographics.read", clinic_id=clinic_id)
        except CurrentActorError as error:
            raise PatientAccessDeniedError from error
        return tuple(
            DuplicateCandidate.objects.filter(clinic_id=clinic_id).order_by(
                "created_at", "id"
            )
        )

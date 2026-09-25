"""Versioned patient demographics, identifiers and correction history.

Demographics versions are append-only rows: ``update_demographics`` writes
the next version under a per-patient advisory lock after a strict
``expected_version`` compare (``demographics_stale`` on mismatch) and always
records a ``DemographicsCorrection`` receipt binding actor, reason and the
previous version. Identifier exact search never feeds ciphertext or
plaintext to SQL: the normalized value is indexed by the tenant-keyed HMAC
returned by ``clinic_app.protected_blind_index``, one digest per DEK
version, so lookups keep working across key rotation. A second patient with
the same active identifier is a ``possible_duplicate`` review hint naming
the matching enrollment, never a silent block or a silent link.

Authorization is permission-based: ``demographics.read`` to view or search,
``demographics.write`` to record or retire, and ``staff.clinic`` for the
clinic intake policy. Legal-name corrections mirror onto the registry row
so the encrypted name search keeps matching the corrected identity.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, cast
from uuid import UUID

from django.db import IntegrityError, transaction
from django.utils import timezone

from apps.audit.services import record_phase1_event
from apps.core.idempotency import PatientNameValueError, normalize_patient_name
from apps.identity.current_context import (
    CurrentActorError,
    current_actor_username,
    require_permission,
)
from apps.identity.models import Clinic
from apps.intake.access import (
    PatientAccessDeniedError,
    authorized_enrollment_for,
)
from apps.intake.models import (
    ADDRESS_KIND_VALUES,
    DEMOGRAPHICS_SOURCE_VALUES,
    GENDER_IDENTITY_VALUES,
    IDENTIFIER_KIND_VALUES,
    SEX_AT_BIRTH_VALUES,
    ClinicIntakePolicy,
    DemographicsCorrection,
    EmergencyContact,
    InsuranceMembership,
    Patient,
    PatientAddress,
    PatientClinicEnrollment,
    PatientDemographics,
    PatientIdentifier,
    QuestionnaireResponse,
)
from apps.intake.patient_search import PatientSearchItem, PatientSearchPage
from apps.scheduling.locks import acquire_advisory_locks, patient_lock_key
from apps.tenancy.envelope import blind_indexes

DEMOGRAPHICS_FIELD_VALUES: Final = (
    "legal_name",
    "social_name",
    "preferred_name",
    "sex_at_birth",
    "gender_identity",
    "pronouns",
    "language",
    "accessibility_needs",
    "occupation",
)
_ADDRESS_FIELDS: Final = (
    "postal_code",
    "street",
    "street_number",
    "complement",
    "district",
    "city",
    "state_code",
)
_CONTACT_FIELDS: Final = ("name", "relationship", "phone")
_MEMBERSHIP_FIELDS: Final = (
    "payer_name",
    "ans_number",
    "membership_number",
    "plan_name",
    "valid_until",
)
IDENTIFIER_INDEX_PURPOSE: Final = "intake.patientidentifier.value"
MAX_TEXT_LENGTH: Final = 255
MAX_REASON_LENGTH: Final = 512
MAX_IDENTIFIER_LENGTH: Final = 64
BLIND_INDEX_LENGTH: Final = 32
CPF_LENGTH: Final = 11
CNS_LENGTH: Final = 15
CPF_FIRST_WEIGHT_BASE: Final = 10
MIN_ALNUM_IDENTIFIER_LENGTH: Final = 2
MAX_ALNUM_IDENTIFIER_LENGTH: Final = 32
CEP_LENGTH: Final = 8
MASK_TAIL: Final = 4
_ASCII_MAX: Final = 128
_CPF_REMAINDER_MIN: Final = 2
HISTORY_LIMIT: Final = 20
MAX_SECTION_ROWS: Final = 3
_UF_VALUES: Final = frozenset(
    {
        "AC",
        "AL",
        "AM",
        "AP",
        "BA",
        "CE",
        "DF",
        "ES",
        "GO",
        "MA",
        "MG",
        "MS",
        "MT",
        "PA",
        "PB",
        "PE",
        "PI",
        "PR",
        "RJ",
        "RN",
        "RO",
        "RR",
        "RS",
        "SC",
        "SE",
        "SP",
        "TO",
    }
)
_CPF_ALL_SAME: Final = re.compile(r"^(\d)\1{10}$")
_PHONE_PATTERN: Final = re.compile(r"^\+?\d{8,15}$")
_CORRECTION_HISTORY_LIMIT: Final = 20


class DemographicsInputError(ValueError):
    """Reject malformed demographics input without reflecting it."""

    def __init__(self) -> None:
        """Expose one stable non-identifying validation message."""
        super().__init__("demographics input is invalid")


class DemographicsStaleError(Exception):
    """Reject a write against a version the actor never saw."""

    def __init__(self, code: str = "demographics_stale") -> None:
        """Expose the stable conflict code the plan's contract names."""
        super().__init__(code)


class DemographicsRequiredError(ValueError):
    """Reject a version that empties a field the clinic policy requires."""

    def __init__(self) -> None:
        """Expose one stable message that names no field values."""
        super().__init__("required demographic fields are missing")


class IdentifierConflictError(Exception):
    """Reject replacing an active identifier through the add path."""

    def __init__(self) -> None:
        """Expose one stable non-identifying conflict message."""
        super().__init__("patient identifier conflict")


@dataclass(frozen=True, slots=True)
class IdentifierOutcome:
    """The recorded identifier plus an optional duplicate-review hint."""

    identifier_id: UUID
    matching_enrollment_id: UUID | None


@dataclass(frozen=True, slots=True)
class CorrectionView:
    """One correction receipt: who, when, reason and changed fields."""

    version: int
    actor_label: str
    reason: str
    changed_fields: tuple[str, ...]
    source: str
    created_at: object


@dataclass(frozen=True, slots=True)
class IdentifierView:
    """One identifier row with the value masked for staff display."""

    kind: str
    masked_value: str
    issuer: str
    version: int
    retired: bool


@dataclass(frozen=True, slots=True)
class AddressView:
    """One address row for the staff profile."""

    kind: str
    postal_code: str
    street: str
    street_number: str
    complement: str
    district: str
    city: str
    state_code: str
    version: int


@dataclass(frozen=True, slots=True)
class EmergencyContactView:
    """One emergency contact slot for the staff profile."""

    sequence: int
    name: str
    relationship: str
    phone: str
    version: int


@dataclass(frozen=True, slots=True)
class MembershipView:
    """One insurance membership slot for the staff profile."""

    sequence: int
    payer_name: str
    ans_number: str
    membership_number: str
    plan_name: str
    valid_until: date | None
    version: int


@dataclass(frozen=True, slots=True)
class DemographicsProfile:
    """Everything the staff profile screen renders for one enrollment."""

    enrollment_id: UUID
    patient_id: UUID
    display_name: str
    version: int
    values: dict[str, str]
    source: str
    required_fields: tuple[str, ...]
    identifiers: tuple[IdentifierView, ...]
    addresses: tuple[AddressView, ...]
    emergency_contacts: tuple[EmergencyContactView, ...]
    memberships: tuple[MembershipView, ...]
    corrections: tuple[CorrectionView, ...]


def _actor_label() -> str:
    """Capture the current actor's username through the trusted resolver."""
    try:
        return current_actor_username()[:150]
    except CurrentActorError as error:
        raise PatientAccessDeniedError from error


def _clean_text(value: object) -> str | None:
    """Validate one optional free-text field to a bounded stripped string."""
    if value is None:
        return None
    if type(value) is not str:
        raise DemographicsInputError
    text = " ".join(value.split())
    if len(text) > MAX_TEXT_LENGTH:
        raise DemographicsInputError
    if any(unicodedata.category(c) in {"Cc", "Cs"} for c in text):
        raise DemographicsInputError
    return text or None


def _clean_name(value: object) -> str | None:
    """Validate one name field; an empty string clears the field."""
    if value is None:
        return None
    if type(value) is not str:
        raise DemographicsInputError
    if not value.strip():
        return None
    try:
        return normalize_patient_name(value)
    except PatientNameValueError as error:
        raise DemographicsInputError from error


def _clean_choice(value: object, allowed: Sequence[str]) -> str | None:
    """Validate one closed-vocabulary or explicit-sentinel field."""
    text = _clean_text(value)
    if text is None:
        return None
    if text not in allowed:
        raise DemographicsInputError
    return text


def _cpf_digits(value: str) -> str | None:
    digits = "".join(c for c in value if c.isdigit() and ord(c) < _ASCII_MAX)
    if len(digits) != CPF_LENGTH or _CPF_ALL_SAME.match(digits):
        return None
    first = (
        sum(int(digits[i]) * (CPF_FIRST_WEIGHT_BASE - i) for i in range(9)) % CPF_LENGTH
    )
    if (0 if first < _CPF_REMAINDER_MIN else CPF_LENGTH - first) != int(digits[9]):
        return None
    second = sum(int(digits[i]) * (CPF_LENGTH - i) for i in range(10)) % CPF_LENGTH
    if (0 if second < _CPF_REMAINDER_MIN else CPF_LENGTH - second) != int(digits[10]):
        return None
    return digits


def normalize_identifier(kind: str, value: object) -> str:
    """Normalize one identifier value for storage and blind indexing.

    CPF/CNS normalize to bare digits (CPF check digits are enforced); RG
    and passport keep alphanumerics case-folded; ``other`` collapses
    whitespace. The blind index is computed over this exact form.
    """
    if kind not in IDENTIFIER_KIND_VALUES or type(value) is not str:
        raise DemographicsInputError
    if kind == "cpf":
        digits = _cpf_digits(value)
        if digits is None:
            raise DemographicsInputError
        return digits
    if kind == "cns":
        digits = "".join(c for c in value if c.isdigit() and ord(c) < _ASCII_MAX)
        if len(digits) != CNS_LENGTH:
            raise DemographicsInputError
        return digits
    if kind in ("rg", "passport"):
        normalized = "".join(unicodedata.normalize("NFKD", value).casefold().split())
        normalized = "".join(c for c in normalized if c.isalnum())
        if not (
            MIN_ALNUM_IDENTIFIER_LENGTH
            <= len(normalized)
            <= MAX_ALNUM_IDENTIFIER_LENGTH
        ):
            raise DemographicsInputError
        return normalized
    freeform = _clean_text(value)
    if not freeform or len(freeform) > MAX_IDENTIFIER_LENGTH:
        raise DemographicsInputError
    return freeform


def _identifier_digests(kind: str, normalized: str) -> tuple[tuple[bytes, int], ...]:
    """Return (digest, key_version) for every tenant DEK version."""
    indexes = blind_indexes(
        purpose=IDENTIFIER_INDEX_PURPOSE,
        plaintext=f"{kind}:{normalized}".encode(),
    )
    return tuple(
        (index.digest, index.key_version)
        for index in sorted(indexes, key=lambda entry: entry.key_version)
    )


def _latest_demographics(
    organization_id: UUID,
    patient_id: UUID,
) -> PatientDemographics | None:
    return (
        PatientDemographics.objects.filter(
            organization_id=organization_id, patient_id=patient_id
        )
        .order_by("-version")
        .first()
    )


def _required_fields(clinic_id: UUID) -> tuple[str, ...]:
    policy = (
        ClinicIntakePolicy.objects.filter(clinic_id=clinic_id)
        .order_by("-version")
        .first()
    )
    if policy is None:
        return ()
    fields = policy.required_fields
    if not isinstance(fields, list):
        return ()
    return tuple(
        str(field) for field in fields if str(field) in DEMOGRAPHICS_FIELD_VALUES
    )


def _merged_values(
    current: PatientDemographics | None,
    changes: Mapping[str, object],
) -> dict[str, str | None]:
    merged: dict[str, str | None] = {}
    for field in DEMOGRAPHICS_FIELD_VALUES:
        existing = getattr(current, field, None) if current is not None else None
        merged[field] = existing or None
    for field, value in changes.items():
        if field in ("legal_name", "social_name", "preferred_name"):
            merged[field] = _clean_name(value)
        elif field in ("sex_at_birth", "gender_identity"):
            merged[field] = _clean_choice(
                value,
                SEX_AT_BIRTH_VALUES
                if field == "sex_at_birth"
                else GENDER_IDENTITY_VALUES,
            )
        else:
            merged[field] = _clean_text(value)
    return merged


def _apply_demographics(  # noqa: PLR0913 - one write needs its full context
    *,
    clinic: Clinic,
    enrollment: PatientClinicEnrollment,
    actor_id: UUID,
    actor_label: str,
    expected_version: int,
    changes: Mapping[str, object],
    reason: str,
    source: str,
) -> PatientDemographics:
    patient = enrollment.patient
    acquire_advisory_locks((patient_lock_key(clinic.organization_id, patient.pk),))
    current = _latest_demographics(clinic.organization_id, patient.pk)
    current_version = current.version if current is not None else 0
    if expected_version != current_version:
        raise DemographicsStaleError
    merged = _merged_values(current, changes)
    # legal_name is never empty on a version row: a first record inherits
    # the registry name, and clearing a recorded name keeps the recorded
    # one rather than inventing a blank identity.
    if merged["legal_name"] is None:
        merged["legal_name"] = (
            (current.legal_name or None) if current is not None else None
        ) or patient.full_name
    missing = [field for field in _required_fields(clinic.pk) if not merged[field]]
    if missing:
        raise DemographicsRequiredError
    new_version = current_version + 1
    try:
        with transaction.atomic():
            row = PatientDemographics.objects.create(
                organization_id=clinic.organization_id,
                patient_id=patient.pk,
                clinic_id=clinic.pk,
                enrollment_id=enrollment.pk,
                version=new_version,
                source=source,
                **merged,
            )
    except IntegrityError as error:
        raise DemographicsStaleError from error
    if merged["legal_name"] and merged["legal_name"] != patient.full_name:
        patient.full_name = merged["legal_name"]
        patient.save(update_fields=("full_name",))
    DemographicsCorrection.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic.pk,
        patient_id=patient.pk,
        demographics=row,
        previous=current,
        actor_id=actor_id,
        actor_label=actor_label,
        reason=reason or None,
        changed_fields=sorted(
            field
            for field in DEMOGRAPHICS_FIELD_VALUES
            if merged[field]
            != (
                (getattr(current, field, None) or None) if current is not None else None
            )
        ),
    )
    record_phase1_event(
        "intake.demographics.saved",
        clinic_id=clinic.pk,
        affected_record_id=row.pk,
    )
    return row


def update_demographics(  # noqa: PLR0913 - the plan's contract fixes these
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    expected_version: int,
    changes: Mapping[str, object],
    reason: str,
    source: str = "staff_recorded",
) -> PatientDemographics:
    """Record one corrected demographics version with its receipt.

    ``expected_version`` is the version the actor reviewed (0 when the
    patient has no demographics yet); a mismatch answers
    ``demographics_stale``. ``changes`` maps demographic field names to
    strings (empty clears); ``sex_at_birth`` and ``gender_identity`` accept
    only the closed vocabulary including ``not_informed``/``declined``.
    """
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    if (
        not isinstance(changes, Mapping)
        or not changes
        or any(field not in DEMOGRAPHICS_FIELD_VALUES for field in changes)
    ):
        raise DemographicsInputError
    if type(reason) is not str or len(reason) > MAX_REASON_LENGTH:
        raise DemographicsInputError
    if source not in DEMOGRAPHICS_SOURCE_VALUES:
        raise DemographicsInputError
    with transaction.atomic():
        actor_id, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        actor_label = _actor_label()
        return _apply_demographics(
            clinic=clinic,
            enrollment=enrollment,
            actor_id=actor_id,
            actor_label=actor_label,
            expected_version=expected_version,
            changes=changes,
            reason=" ".join(reason.split()),
            source=source,
        )


def carry_forward_demographics(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    reason: str,
) -> PatientDemographics | None:
    """Carry the latest questionnaire answers into demographics unverified.

    Only answers whose keys name demographic fields are carried, and the
    new version is recorded with ``source='patient_reported'`` so staff
    surfaces present it as reported by the patient and unverified. A
    response without mappable answers changes nothing.
    """
    with transaction.atomic():
        actor_id, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        response = (
            QuestionnaireResponse.objects.filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                enrollment_id=enrollment.pk,
                state="submitted",
            )
            .order_by("-revision")
            .first()
        )
        if response is None:
            return None
        answers = response.answers
        if not isinstance(answers, dict):
            return None
        # Questionnaire ids are constrained to the q_* prefix; an intake
        # template names a demographic field as q_<field>.
        changes = {
            key[2:]: value
            for key, value in answers.items()
            if type(key) is str
            and key.startswith("q_")
            and key[2:] in DEMOGRAPHICS_FIELD_VALUES
            and (value is None or type(value) is str)
        }
        if not changes:
            return None
        return _apply_demographics(
            clinic=clinic,
            enrollment=enrollment,
            actor_id=actor_id,
            actor_label=_actor_label(),
            expected_version=(
                current.version
                if (
                    current := _latest_demographics(
                        clinic.organization_id, enrollment.patient_id
                    )
                )
                is not None
                else 0
            ),
            changes=changes,
            reason=" ".join(reason.split()),
            source="patient_reported",
        )


def add_identifier(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    kind: str,
    value: object,
    issuer: object = None,
) -> IdentifierOutcome:
    """Record one identifier; surface same-org duplicates for review.

    The normalized value is stored as an envelope and indexed by the
    tenant-keyed blind index per kind. When another patient in the same
    organization already holds the same active identifier the record is
    still written and the outcome names the earliest enrollment of the
    matching patient so review can adjudicate (todo 18).
    """
    normalized = normalize_identifier(kind, value)
    issuer_text = _clean_text(issuer)
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        acquire_advisory_locks(
            (patient_lock_key(clinic.organization_id, enrollment.patient_id),)
        )
        digests = _identifier_digests(kind, normalized)
        digest, key_version = digests[-1]
        existing = (
            PatientIdentifier.objects.filter(
                organization_id=clinic.organization_id,
                patient_id=enrollment.patient_id,
                kind=kind,
            )
            .order_by("created_at", "id")
            .first()
        )
        if existing is not None and existing.retired_at is None:
            raise IdentifierConflictError
        duplicate = (
            PatientIdentifier.objects.filter(
                organization_id=clinic.organization_id,
                kind=kind,
                blind_index__in=[candidate for candidate, _ in digests],
                retired_at__isnull=True,
            )
            .exclude(patient_id=enrollment.patient_id)
            .order_by("created_at", "id")
            .first()
        )
        matching_enrollment_id = None
        if duplicate is not None:
            match = (
                PatientClinicEnrollment.objects.filter(
                    organization_id=clinic.organization_id,
                    patient_id=duplicate.patient_id,
                )
                .order_by("created_at", "id")
                .first()
            )
            matching_enrollment_id = match.pk if match is not None else None
        if existing is None:
            identifier = PatientIdentifier.objects.create(
                organization_id=clinic.organization_id,
                patient_id=enrollment.patient_id,
                clinic_id=clinic.pk,
                kind=kind,
                value=normalized,
                blind_index=digest,
                index_key_version=key_version,
                issuer=issuer_text,
            )
        else:
            identifier = existing
            identifier.value = normalized
            identifier.blind_index = digest
            identifier.index_key_version = key_version
            identifier.issuer = issuer_text
            identifier.version += 1
            identifier.retired_at = None
            identifier.save(
                update_fields=(
                    "value",
                    "blind_index",
                    "index_key_version",
                    "issuer",
                    "version",
                    "retired_at",
                    "updated_at",
                )
            )
        record_phase1_event(
            "intake.identifier.saved",
            clinic_id=clinic.pk,
            affected_record_id=identifier.pk,
        )
        return IdentifierOutcome(
            identifier_id=identifier.pk,
            matching_enrollment_id=matching_enrollment_id,
        )


def retire_identifier(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    kind: str,
    expected_version: int,
) -> PatientIdentifier:
    """Retire one active identifier without deleting its history."""
    if kind not in IDENTIFIER_KIND_VALUES:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 1:
        raise DemographicsInputError
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        acquire_advisory_locks(
            (patient_lock_key(clinic.organization_id, enrollment.patient_id),)
        )
        identifier = (
            PatientIdentifier.objects.filter(
                organization_id=clinic.organization_id,
                patient_id=enrollment.patient_id,
                kind=kind,
                retired_at__isnull=True,
            )
            .order_by("created_at", "id")
            .first()
        )
        if identifier is None:
            raise DemographicsInputError
        if identifier.version != expected_version:
            raise DemographicsStaleError
        identifier.retired_at = timezone.now()
        identifier.version += 1
        identifier.save(update_fields=("retired_at", "version", "updated_at"))
        record_phase1_event(
            "intake.identifier.retired",
            clinic_id=clinic.pk,
            affected_record_id=identifier.pk,
        )
        return identifier


def _identifier_matches(
    organization_id: UUID,
    kind: str,
    normalized: str,
) -> list[UUID]:
    digests = _identifier_digests(kind, normalized)
    return list(
        PatientIdentifier.objects.filter(
            organization_id=organization_id,
            kind=kind,
            blind_index__in=[digest for digest, _ in digests],
            retired_at__isnull=True,
        ).values_list("patient_id", flat=True)
    )


def search_patient_identifiers(
    *,
    clinic_id: UUID,
    kind: str,
    value: object,
) -> PatientSearchPage:
    """Return enrollments whose patients hold one exact active identifier.

    The lookup binds organization, kind and tenant-keyed blind index; a
    cross-tenant probe matches nothing because its digest derives from a
    different DEK. Result shape reuses the registry page contract.
    """
    normalized = normalize_identifier(kind, value)
    with transaction.atomic():
        try:
            require_permission("demographics.read", clinic_id=clinic_id)
            clinic = Clinic.objects.get(pk=clinic_id)
        except (CurrentActorError, Clinic.DoesNotExist) as error:
            raise PatientAccessDeniedError from error
        patient_ids = _identifier_matches(clinic.organization_id, kind, normalized)
        enrollments = (
            PatientClinicEnrollment.objects.select_related("patient")
            .filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                patient_id__in=patient_ids,
            )
            .order_by("created_at", "id")[:100]
        )
        demographics = {
            row.patient_id: row
            for row in PatientDemographics.objects.filter(
                organization_id=clinic.organization_id,
                patient_id__in=[e.patient_id for e in enrollments],
            ).order_by("-version")
        }
        items = tuple(
            PatientSearchItem(
                enrollment_id=enrollment.pk,
                full_name=enrollment.patient.full_name,
                birth_date=enrollment.patient.birth_date,
                display_name=_display_name(
                    enrollment.patient, demographics.get(enrollment.patient_id)
                ),
            )
            for enrollment in enrollments
        )
        record_phase1_event(
            "intake.patient.searched",
            clinic_id=clinic_id,
            affected_record_id=clinic_id,
        )
        return PatientSearchPage(
            items=items,
            page=1,
            total=len(items),
            page_count=1 if items else 0,
        )


def _display_name(patient: Patient, row: PatientDemographics | None) -> str:
    """Prefer the social name, then the legal or registry name."""
    if row is not None:
        for candidate in (row.social_name, row.legal_name):
            if isinstance(candidate, str) and candidate:
                return candidate
    return str(patient.full_name)


def demographics_profile(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
) -> DemographicsProfile:
    """Return the full staff profile for one enrolled patient."""
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.read"
        )
        organization_id = clinic.organization_id
        patient_id = enrollment.patient_id
        current = _latest_demographics(organization_id, patient_id)
        identifiers = tuple(
            IdentifierView(
                kind=row.kind,
                masked_value=_mask_identifier(row.kind, row.value),
                issuer=row.issuer or "",
                version=row.version,
                retired=row.retired_at is not None,
            )
            for row in PatientIdentifier.objects.filter(
                organization_id=organization_id, patient_id=patient_id
            ).order_by("kind")
        )
        addresses = tuple(
            AddressView(
                kind=row.kind,
                postal_code=row.postal_code or "",
                street=row.street or "",
                street_number=row.street_number or "",
                complement=row.complement or "",
                district=row.district or "",
                city=row.city or "",
                state_code=row.state_code or "",
                version=row.version,
            )
            for row in PatientAddress.objects.filter(
                organization_id=organization_id,
                patient_id=patient_id,
                retired_at__isnull=True,
            ).order_by("kind")
        )
        contacts = tuple(
            EmergencyContactView(
                sequence=row.sequence,
                name=row.name or "",
                relationship=row.relationship or "",
                phone=row.phone or "",
                version=row.version,
            )
            for row in EmergencyContact.objects.filter(
                organization_id=organization_id,
                patient_id=patient_id,
                retired_at__isnull=True,
            ).order_by("sequence")
        )
        memberships = tuple(
            MembershipView(
                sequence=row.sequence,
                payer_name=row.payer_name or "",
                ans_number=row.ans_number or "",
                membership_number=row.membership_number or "",
                plan_name=row.plan_name or "",
                valid_until=row.valid_until,
                version=row.version,
            )
            for row in InsuranceMembership.objects.filter(
                organization_id=organization_id,
                patient_id=patient_id,
                retired_at__isnull=True,
            ).order_by("sequence")
        )
        corrections = tuple(
            CorrectionView(
                version=correction.demographics.version,
                actor_label=correction.actor_label,
                reason=correction.reason or "",
                changed_fields=tuple(str(field) for field in correction.changed_fields),
                source=correction.demographics.source,
                created_at=correction.created_at,
            )
            for correction in DemographicsCorrection.objects.select_related(
                "demographics"
            )
            .filter(organization_id=organization_id, patient_id=patient_id)
            .order_by("-created_at")[:_CORRECTION_HISTORY_LIMIT]
        )
        record_phase1_event(
            "intake.demographics.viewed",
            clinic_id=clinic_id,
            affected_record_id=enrollment.pk,
        )
        return DemographicsProfile(
            enrollment_id=enrollment.pk,
            patient_id=patient_id,
            display_name=_display_name(enrollment.patient, current),
            version=current.version if current is not None else 0,
            values={
                field: getattr(current, field, None) or ""
                for field in DEMOGRAPHICS_FIELD_VALUES
            },
            source=current.source if current is not None else "",
            required_fields=_required_fields(clinic.pk),
            identifiers=identifiers,
            addresses=addresses,
            emergency_contacts=contacts,
            memberships=memberships,
            corrections=corrections,
        )


@dataclass(frozen=True, slots=True)
class BillingDemographics:
    """The minimum identity a finance role may resolve for one enrollment."""

    enrollment_id: UUID
    patient_id: UUID
    display_name: str
    birth_date: date
    identifiers: tuple[IdentifierView, ...]
    memberships: tuple[MembershipView, ...]


def billing_demographics(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
) -> BillingDemographics:
    """Return the narrow read a finance role needs for billing identity.

    Binds ``demographics.billing_read`` (finance and no wider staff role):
    the enrolled patient's display name, birth date, identifiers and payer
    memberships; the rest of the demographics record stays outside this
    surface.
    """
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.billing_read"
        )
        organization_id = clinic.organization_id
        patient_id = enrollment.patient_id
        current = _latest_demographics(organization_id, patient_id)
        identifiers = tuple(
            IdentifierView(
                kind=row.kind,
                masked_value=_mask_identifier(row.kind, row.value),
                issuer=row.issuer or "",
                version=row.version,
                retired=row.retired_at is not None,
            )
            for row in PatientIdentifier.objects.filter(
                organization_id=organization_id, patient_id=patient_id
            ).order_by("kind")
        )
        memberships = tuple(
            MembershipView(
                sequence=row.sequence,
                payer_name=row.payer_name or "",
                ans_number=row.ans_number or "",
                membership_number=row.membership_number or "",
                plan_name=row.plan_name or "",
                valid_until=row.valid_until,
                version=row.version,
            )
            for row in InsuranceMembership.objects.filter(
                organization_id=organization_id,
                patient_id=patient_id,
                retired_at__isnull=True,
            ).order_by("sequence")
        )
        record_phase1_event(
            "intake.demographics.billing_viewed",
            clinic_id=clinic_id,
            affected_record_id=enrollment.pk,
        )
        return BillingDemographics(
            enrollment_id=enrollment.pk,
            patient_id=patient_id,
            display_name=_display_name(enrollment.patient, current),
            birth_date=enrollment.patient.birth_date,
            identifiers=identifiers,
            memberships=memberships,
        )


def _mask_identifier(kind: str, value: str) -> str:
    """Mask one stored identifier for the profile list."""
    tail = value[-MASK_TAIL:] if len(value) >= MASK_TAIL else value
    if kind in ("cpf", "cns"):
        return f"••• {tail}"
    return f"••••{tail}"


_SECTION_MODELS = PatientAddress | EmergencyContact | InsuranceMembership


def _save_section(  # noqa: PLR0913 - one section write needs its context
    *,
    model: type[_SECTION_MODELS],
    lookup: dict[str, object],
    defaults: dict[str, object] | None,
    clinic: Clinic,
    patient_id: UUID,
    expected_version: int,
    audit_type: str,
) -> _SECTION_MODELS:
    """Create or CAS-update one per-kind/sequence row; ``None`` retires it."""
    row = (
        model.objects.filter(
            organization_id=clinic.organization_id,
            patient_id=patient_id,
            **lookup,
        )
        .order_by("created_at", "id")
        .first()
    )
    if defaults is None:
        if row is None or row.retired_at is not None:
            raise DemographicsInputError
        if row.version != expected_version:
            raise DemographicsStaleError
        row.retired_at = timezone.now()
        row.version += 1
        row.save(update_fields=("retired_at", "version", "updated_at"))
        verb = "retired"
    elif row is None:
        if expected_version != 0:
            raise DemographicsStaleError
        row = model.objects.create(
            organization_id=clinic.organization_id,
            patient_id=patient_id,
            **lookup,
            **defaults,
        )
        verb = "saved"
    else:
        if row.version != expected_version or row.retired_at is not None:
            raise DemographicsStaleError
        for field, value in defaults.items():
            setattr(row, field, value)
        row.version += 1
        row.save(update_fields=(*defaults, "version", "updated_at"))
        verb = "saved"
    record_phase1_event(
        audit_type if verb == "saved" else f"{audit_type.removesuffix('saved')}retired",
        clinic_id=clinic.pk,
        affected_record_id=row.pk,
    )
    return row


def save_patient_address(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    kind: str,
    expected_version: int,
    address: Mapping[str, object] | None,
) -> PatientAddress:
    """Create, replace or retire one address of a kind.

    ``address`` maps the address fields to strings; ``None`` retires the
    row. ``expected_version`` is the rendered row version (0 to create).
    """
    if kind not in ADDRESS_KIND_VALUES:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    if address is not None:
        if not isinstance(address, Mapping) or set(address) - set(_ADDRESS_FIELDS):
            raise DemographicsInputError
        values: dict[str, object] = {
            field: _clean_text(address.get(field)) for field in _ADDRESS_FIELDS
        }
        postal = values["postal_code"]
        if isinstance(postal, str):
            digits = "".join(c for c in postal if c.isdigit())
            if len(digits) != CEP_LENGTH:
                raise DemographicsInputError
            values["postal_code"] = digits
        state = values["state_code"]
        if state is not None and state not in _UF_VALUES:
            raise DemographicsInputError
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        acquire_advisory_locks(
            (patient_lock_key(clinic.organization_id, enrollment.patient_id),)
        )
        return cast(
            "PatientAddress",
            _save_section(
                model=PatientAddress,
                lookup={"kind": kind},
                defaults=None if address is None else {**values, "retired_at": None},
                clinic=clinic,
                patient_id=enrollment.patient_id,
                expected_version=expected_version,
                audit_type="intake.address.saved",
            ),
        )


def save_emergency_contact(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    sequence: int,
    expected_version: int,
    contact: Mapping[str, object] | None,
) -> EmergencyContact:
    """Create, replace or retire one emergency contact slot (1-3)."""
    if type(sequence) is not int or not 1 <= sequence <= MAX_SECTION_ROWS:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    values: dict[str, object] = {}
    if contact is not None:
        if not isinstance(contact, Mapping) or set(contact) - set(_CONTACT_FIELDS):
            raise DemographicsInputError
        values = {field: _clean_text(contact.get(field)) for field in _CONTACT_FIELDS}
        phone = values["phone"]
        if phone is not None:
            if not isinstance(phone, str):
                raise DemographicsInputError
            digits = re.sub(r"[\s().\-]", "", phone)
            if _PHONE_PATTERN.fullmatch(digits) is None:
                raise DemographicsInputError
            values["phone"] = digits
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        acquire_advisory_locks(
            (patient_lock_key(clinic.organization_id, enrollment.patient_id),)
        )
        return cast(
            "EmergencyContact",
            _save_section(
                model=EmergencyContact,
                lookup={"sequence": sequence},
                defaults=None if contact is None else {**values, "retired_at": None},
                clinic=clinic,
                patient_id=enrollment.patient_id,
                expected_version=expected_version,
                audit_type="intake.emergency_contact.saved",
            ),
        )


def save_insurance_membership(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    sequence: int,
    expected_version: int,
    membership: Mapping[str, object] | None,
) -> InsuranceMembership:
    """Create, replace or retire one insurance membership slot (1-3)."""
    if type(sequence) is not int or not 1 <= sequence <= MAX_SECTION_ROWS:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    values: dict[str, object] = {}
    if membership is not None:
        if not isinstance(membership, Mapping) or set(membership) - set(
            _MEMBERSHIP_FIELDS
        ):
            raise DemographicsInputError
        values = {}
        for field in _MEMBERSHIP_FIELDS:
            raw = membership.get(field)
            if field == "valid_until":
                if raw is not None and type(raw) is not date:
                    raise DemographicsInputError
                values[field] = raw
            else:
                values[field] = _clean_text(raw)
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        acquire_advisory_locks(
            (patient_lock_key(clinic.organization_id, enrollment.patient_id),)
        )
        return cast(
            "InsuranceMembership",
            _save_section(
                model=InsuranceMembership,
                lookup={"sequence": sequence},
                defaults=None if membership is None else {**values, "retired_at": None},
                clinic=clinic,
                patient_id=enrollment.patient_id,
                expected_version=expected_version,
                audit_type="intake.membership.saved",
            ),
        )


def set_intake_policy(
    *,
    clinic_id: UUID,
    required_fields: Sequence[str],
) -> ClinicIntakePolicy:
    """Publish the next clinic intake-policy version.

    ``required_fields`` is a closed list of demographic field names that a
    demographics write must leave non-empty; the explicit unknown/declined
    sentinels satisfy the requirement so no invented values are forced.
    """
    fields = tuple(str(field) for field in required_fields)
    if any(field not in DEMOGRAPHICS_FIELD_VALUES for field in fields):
        raise DemographicsInputError
    with transaction.atomic():
        try:
            actor_id = require_permission("staff.clinic", clinic_id=clinic_id)
            clinic = Clinic.objects.get(pk=clinic_id)
        except (CurrentActorError, Clinic.DoesNotExist) as error:
            raise PatientAccessDeniedError from error
        latest = (
            ClinicIntakePolicy.objects.filter(clinic_id=clinic.pk)
            .order_by("-version")
            .first()
        )
        policy = ClinicIntakePolicy.objects.create(
            organization_id=clinic.organization_id,
            clinic_id=clinic.pk,
            version=(latest.version if latest is not None else 0) + 1,
            required_fields=list(dict.fromkeys(fields)),
            published_by_id=UUID(str(actor_id)),
        )
        record_phase1_event(
            "intake.intake_policy.saved",
            clinic_id=clinic.pk,
            affected_record_id=policy.pk,
        )
        return policy

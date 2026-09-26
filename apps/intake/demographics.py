"""Versioned patient demographics, identifiers and correction history.

Every identity record is append-only history. ``update_demographics`` writes
the next demographics version under a per-patient advisory lock after a
strict ``expected_version`` compare (``demographics_stale`` on mismatch) and
always records a ``DemographicsCorrection`` receipt binding actor, reason
and the previous version. Identifiers, addresses, emergency contacts and
insurance memberships are versioned the same way: a save or retirement
appends the next version of its kind/slot, so replaced values stay on
record; the database refuses UPDATE/DELETE and out-of-order versions.

Nothing is invented. Every field is optional unless the clinic intake
policy requires it, and a field left empty on purpose records an explicit
``not_informed``/``declined`` answer (the coded fields carry these in their
vocabulary). The one structural rule is that a patient keeps a legal or a
social name so staff can address and find them; the registry row mirrors
that name and the (possibly unknown) birth date of the latest version.

Identifier exact search never feeds ciphertext or plaintext to SQL: the
normalized value is indexed by the tenant-keyed HMAC returned by
``clinic_app.protected_blind_index``, one digest per DEK version, so lookups
keep working across key rotation. A second patient with the same active
identifier is a ``possible_duplicate`` review hint naming the matching
enrollment, never a silent block or a silent link.

Authorization: registration writes its first version under the same
manager authority as ``create_patient``; afterwards ``demographics.read``
views or searches and ``demographics.write`` corrects or retires. The
clinic intake policy is clinic configuration: ``configuration.clinic`` or
``configuration.organization``.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Final, cast
from uuid import UUID

from django.db import IntegrityError, connection, transaction
from django.db.models import Exists, OuterRef, QuerySet
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
    authorized_enrollment,
    authorized_enrollment_for,
    authorized_manager_clinic,
)
from apps.intake.models import (
    ADDRESS_KIND_VALUES,
    DEMOGRAPHICS_SOURCE_VALUES,
    GENDER_IDENTITY_VALUES,
    IDENTIFIER_KIND_VALUES,
    SEX_AT_BIRTH_VALUES,
    UNKNOWN_DEMOGRAPHIC_VALUES,
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
from apps.intake.patient_creation import (
    PatientBirthDateError,
    PatientIdempotencyConflictError,
    PatientRegistration,
    create_patient,
    validate_birth_date,
)
from apps.intake.patient_search import PatientSearchItem, PatientSearchPage
from apps.scheduling.locks import acquire_advisory_locks, patient_lock_key
from apps.tenancy.envelope import blind_indexes, protect

DEMOGRAPHICS_FIELD_VALUES: Final = (
    "legal_name",
    "social_name",
    "preferred_name",
    "birth_date",
    "sex_at_birth",
    "gender_identity",
    "pronouns",
    "language",
    "accessibility_needs",
    "occupation",
)
NAME_FIELDS: Final = ("legal_name", "social_name", "preferred_name")
CODED_FIELDS: Final[Mapping[str, Sequence[str]]] = {
    "sex_at_birth": SEX_AT_BIRTH_VALUES,
    "gender_identity": GENDER_IDENTITY_VALUES,
}
# Fields whose deliberate non-answer lives in the version's unknown map; the
# coded fields carry not_informed/declined in their own vocabulary.
STATUS_FIELDS: Final = tuple(
    field for field in DEMOGRAPHICS_FIELD_VALUES if field not in CODED_FIELDS
)
# Registration takes these as explicit inputs; the rest arrive as changes.
_REGISTRATION_FIELDS: Final = frozenset({"legal_name", "social_name", "birth_date"})
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
SECTION_SLOTS: Final = tuple(range(1, MAX_SECTION_ROWS + 1))
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
_BR_DATE: Final = re.compile(r"(\d{2})/(\d{2})/(\d{4})")
# A deliberate non-answer is information: "declined" says more than
# "not_informed", which says more than nothing at all.
_NON_ANSWER_RANK: Final[Mapping[str | None, int]] = {
    None: 0,
    "not_informed": 1,
    "declined": 2,
}
_PHONE_PATTERN: Final = re.compile(r"^\+?\d{8,15}$")
_CORRECTION_HISTORY_LIMIT: Final = 20

type FieldValue = str | date | None


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
    """Reject a version that leaves a policy-required field unanswered."""

    def __init__(self) -> None:
        """Expose one stable message that names no field values."""
        super().__init__("required demographic fields are missing")


class DemographicsNameRequiredError(ValueError):
    """Reject a version that would leave the patient with no name at all."""

    def __init__(self) -> None:
        """Expose one stable message that names no field values."""
        super().__init__("a legal or social name is required")


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
class NotCarried:
    """One questionnaire answer carry-forward skipped, and why."""

    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class CarryForwardOutcome:
    """The version a carry-forward wrote (if any) and the skipped answers."""

    version: PatientDemographics | None
    not_carried: tuple[NotCarried, ...]


@dataclass(frozen=True, slots=True)
class RegistrationOutcome:
    """One registration and the enrollment a document duplicate points to."""

    registration: PatientRegistration
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
    """The current version of one identifier kind, value masked."""

    kind: str
    masked_value: str
    issuer: str
    version: int
    retired: bool


@dataclass(frozen=True, slots=True)
class AddressView:
    """The current address of one kind for the staff profile."""

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
    """The current emergency contact of one slot for the staff profile."""

    sequence: int
    name: str
    relationship: str
    phone: str
    version: int


@dataclass(frozen=True, slots=True)
class MembershipView:
    """The current insurance membership of one slot for the staff profile."""

    sequence: int
    payer_name: str
    ans_number: str
    membership_number: str
    plan_name: str
    valid_until: date | None
    version: int


@dataclass(frozen=True, slots=True)
class DemographicsProfile:
    """Everything the staff profile screen renders for one enrollment.

    ``values`` holds form-ready strings (ISO date for ``birth_date``);
    ``unknown`` maps a deliberately unanswered field to its status code.
    The ``free_*`` tuples list the kinds/slots with no current row.
    """

    enrollment_id: UUID
    patient_id: UUID
    display_name: str
    version: int
    values: dict[str, str]
    unknown: dict[str, str]
    birth_date: date | None
    source: str
    required_fields: tuple[str, ...]
    identifiers: tuple[IdentifierView, ...]
    addresses: tuple[AddressView, ...]
    emergency_contacts: tuple[EmergencyContactView, ...]
    memberships: tuple[MembershipView, ...]
    free_address_kinds: tuple[str, ...]
    free_contact_slots: tuple[int, ...]
    free_membership_slots: tuple[int, ...]
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
    """Validate one closed-vocabulary field, sentinels included."""
    text = _clean_text(value)
    if text is None:
        return None
    if text not in allowed:
        raise DemographicsInputError
    return text


def _clean_birth_date(value: object, clinic: Clinic) -> date | None:
    """Validate one optional birth date; never after the clinic's today."""
    if value is None or value == "":
        return None
    if type(value) is str:
        # ISO (form inputs) or the pt-BR DD/MM/AAAA a patient types.
        match = _BR_DATE.fullmatch(value.strip())
        text = f"{match[3]}-{match[2]}-{match[1]}" if match else value
        try:
            value = date.fromisoformat(text)
        except ValueError as error:
            raise DemographicsInputError from error
    if type(value) is not date:
        raise DemographicsInputError
    try:
        validate_birth_date(value, clinic)
    except PatientBirthDateError as error:
        raise DemographicsInputError from error
    return value


def _clean_field(field: str, value: object, clinic: Clinic) -> FieldValue:
    if field in NAME_FIELDS:
        return _clean_name(value)
    if field == "birth_date":
        return _clean_birth_date(value, clinic)
    if field in CODED_FIELDS:
        return _clean_choice(value, CODED_FIELDS[field])
    return _clean_text(value)


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


def _version_values(
    current: PatientDemographics | None, patient: Patient
) -> dict[str, FieldValue]:
    """Return one version's field values with empty text as ``None``.

    Before the first version the registry row is the recorded identity, so
    its name and birth date seed the values instead of reading as empty.
    """
    if current is None:
        values: dict[str, FieldValue] = dict.fromkeys(DEMOGRAPHICS_FIELD_VALUES)
        values["legal_name"] = patient.full_name or None
        values["birth_date"] = patient.birth_date
        return values
    return {
        field: getattr(current, field, None) or None
        for field in DEMOGRAPHICS_FIELD_VALUES
    }


def _version_unknown(current: PatientDemographics | None) -> dict[str, str]:
    """Return one version's deliberate non-answers, dropping malformed keys."""
    stored = current.unknown_fields if current is not None else None
    if not isinstance(stored, dict):
        return {}
    return {
        str(field): str(status)
        for field, status in stored.items()
        if field in STATUS_FIELDS and status in UNKNOWN_DEMOGRAPHIC_VALUES
    }


def _merged_version(
    current: PatientDemographics | None,
    patient: Patient,
    changes: Mapping[str, object],
    unknown: Mapping[str, object],
    clinic: Clinic,
) -> tuple[dict[str, FieldValue], dict[str, str]]:
    """Apply changed values, then deliberate non-answers, to the current one.

    Recording a value clears that field's non-answer; marking a field
    ``not_informed``/``declined`` requires its value to be empty, so a
    version never holds both. An empty status clears a non-answer.
    """
    values = _version_values(current, patient)
    statuses = _version_unknown(current)
    for field, value in changes.items():
        values[field] = _clean_field(field, value, clinic)
        if values[field] is not None:
            statuses.pop(field, None)
    for field, status in unknown.items():
        if field not in STATUS_FIELDS:
            raise DemographicsInputError
        if status in (None, ""):
            statuses.pop(field, None)
            continue
        if status not in UNKNOWN_DEMOGRAPHIC_VALUES or values[field] is not None:
            raise DemographicsInputError
        statuses[field] = str(status)
    return values, statuses


def _validate_changes(changes: object, unknown: object) -> None:
    if not isinstance(changes, Mapping) or any(
        field not in DEMOGRAPHICS_FIELD_VALUES for field in changes
    ):
        raise DemographicsInputError
    if not isinstance(unknown, Mapping):
        raise DemographicsInputError
    if not changes and not unknown:
        raise DemographicsInputError


def _registry_mirror(
    patient: Patient, registry_name: str, birth_date: date | None
) -> tuple[bytes, bytes | None] | None:
    """Return the registry envelopes to mirror, or ``None`` when unchanged.

    A changed column is encrypted once; an unchanged one keeps its stored
    envelope, so the version and the registry row carry identical bytes.
    """
    if registry_name == patient.full_name and birth_date == patient.birth_date:
        return None
    with connection.cursor() as cursor:
        cursor.execute(
            "SELECT full_name, birth_date FROM clinic_app.intake_patient WHERE id = %s",
            [patient.pk],
        )
        stored = cursor.fetchone()
    if stored is None:
        raise DemographicsStaleError
    name = (
        bytes(stored[0])
        if registry_name == patient.full_name
        else protect(
            purpose="intake.patient.full_name", plaintext=registry_name.encode()
        )
    )
    if birth_date == patient.birth_date:
        birth = None if stored[1] is None else bytes(stored[1])
    else:
        birth = (
            None
            if birth_date is None
            else protect(
                purpose="intake.patient.birth_date",
                plaintext=birth_date.isoformat().encode("ascii"),
            )
        )
    return name, birth


def _apply_demographics(  # noqa: PLR0913 - one write needs its full context
    *,
    clinic: Clinic,
    enrollment: PatientClinicEnrollment,
    actor_id: UUID,
    actor_label: str,
    expected_version: int,
    changes: Mapping[str, object],
    unknown: Mapping[str, object],
    reason: str,
    source: str,
) -> PatientDemographics:
    patient = enrollment.patient
    acquire_advisory_locks((patient_lock_key(clinic.organization_id, patient.pk),))
    current = _latest_demographics(clinic.organization_id, patient.pk)
    current_version = current.version if current is not None else 0
    if expected_version != current_version:
        raise DemographicsStaleError
    values, statuses = _merged_version(current, patient, changes, unknown, clinic)
    # The patient must stay addressable: a legal or a social name, never an
    # invented placeholder and never a silent carry-over of a cleared name.
    registry_name = values["legal_name"] or values["social_name"]
    if not isinstance(registry_name, str):
        raise DemographicsNameRequiredError
    missing = [
        field
        for field in _required_fields(clinic.pk)
        if values[field] is None and field not in statuses
    ]
    if missing:
        raise DemographicsRequiredError
    previous_values = _version_values(current, patient)
    previous_statuses = _version_unknown(current)
    birth_date = values["birth_date"]
    mirror = _registry_mirror(
        patient,
        registry_name,
        birth_date if isinstance(birth_date, date) else None,
    )
    try:
        with transaction.atomic():
            row = PatientDemographics.objects.create(
                organization_id=clinic.organization_id,
                patient_id=patient.pk,
                clinic_id=clinic.pk,
                enrollment_id=enrollment.pk,
                version=current_version + 1,
                source=source,
                unknown_fields=statuses or None,
                registry_full_name=mirror[0] if mirror else None,
                registry_birth_date=mirror[1] if mirror else None,
                **values,
            )
    except IntegrityError as error:
        raise DemographicsStaleError from error
    if mirror:
        # The registry row takes the exact envelopes this version recorded;
        # the database admits the change only when they are byte-identical.
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE clinic_app.intake_patient "
                "SET full_name = %s, birth_date = %s WHERE id = %s",
                [mirror[0], mirror[1], patient.pk],
            )
        patient.full_name = registry_name
        patient.birth_date = birth_date if isinstance(birth_date, date) else None
    DemographicsCorrection.objects.create(
        organization_id=clinic.organization_id,
        clinic_id=clinic.pk,
        patient_id=patient.pk,
        demographics=row,
        previous=current,
        actor_id=actor_id,
        actor_label=actor_label,
        reason=reason or None,
        changed_fields=[
            field
            for field in DEMOGRAPHICS_FIELD_VALUES
            if values[field] != previous_values[field]
            or statuses.get(field) != previous_statuses.get(field)
        ],
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
    unknown: Mapping[str, object] | None = None,
) -> PatientDemographics:
    """Record one corrected demographics version with its receipt.

    ``expected_version`` is the version the actor reviewed (0 when the
    patient has no demographics yet); a mismatch answers
    ``demographics_stale``. ``changes`` maps demographic field names to new
    values (strings; a ``date`` or ISO string for ``birth_date``; empty
    clears); ``sex_at_birth`` and ``gender_identity`` accept their closed
    vocabulary including ``not_informed``/``declined``. ``unknown`` marks
    other fields as deliberately ``not_informed``/``declined`` (empty
    clears the mark).
    """
    unknown = {} if unknown is None else unknown
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    _validate_changes(changes, unknown)
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
            unknown=unknown,
            reason=" ".join(reason.split()),
            source=source,
        )


def registration_required_fields(*, clinic_id: UUID) -> tuple[str, ...]:
    """Return the fields the clinic policy requires at registration."""
    with transaction.atomic():
        clinic = authorized_manager_clinic(clinic_id)
        return _required_fields(clinic.pk)


def register_patient(  # noqa: PLR0913 - one registration carries every input
    *,
    clinic_id: UUID,
    idempotency_key: UUID,
    legal_name: str,
    social_name: str,
    birth_date: date | None,
    changes: Mapping[str, object] | None = None,
    unknown: Mapping[str, object] | None = None,
    identifier_kind: str = "",
    identifier_value: str = "",
) -> RegistrationOutcome:
    """Register one patient with their first demographics version.

    Nothing is required beyond a legal or a social name, unless the clinic
    policy says so; ``unknown`` records deliberate non-answers (e.g. a
    ``declined`` birth date). The registry name is the legal name, or the
    social name when no legal name was given. The first version and any
    document are part of the registration act, so they are written under
    the same manager authority as ``create_patient``. An equal replay of
    ``idempotency_key`` returns the first registration unchanged.
    """
    changes = {} if changes is None else changes
    unknown = {} if unknown is None else unknown
    _validate_changes({**changes, "legal_name": legal_name}, unknown)
    if _REGISTRATION_FIELDS & set(changes):
        raise DemographicsInputError
    legal = _clean_name(legal_name)
    social = _clean_name(social_name)
    registry_name = legal or social
    if registry_name is None:
        raise DemographicsNameRequiredError
    normalized = (
        normalize_identifier(identifier_kind, identifier_value)
        if identifier_value
        else None
    )
    with transaction.atomic():
        registration = create_patient(
            clinic_id=clinic_id,
            full_name=registry_name,
            birth_date=birth_date,
            idempotency_key=idempotency_key,
        )
        first = (
            PatientDemographics.objects.filter(
                organization_id=registration.patient.organization_id,
                patient_id=registration.patient.pk,
            )
            .order_by("version")
            .first()
        )
        if first is not None:
            # create_patient's fingerprint binds only the registry name, so
            # a replay that swaps legal and social names would match it; the
            # first version records both and must match too.
            if (first.legal_name or None, first.social_name or None) != (legal, social):
                raise PatientIdempotencyConflictError
            return RegistrationOutcome(
                registration=registration, matching_enrollment_id=None
            )
        actor_id, clinic, enrollment = authorized_enrollment(
            clinic_id, registration.enrollment.pk
        )
        _apply_demographics(
            clinic=clinic,
            enrollment=enrollment,
            actor_id=actor_id,
            actor_label=_actor_label(),
            expected_version=0,
            changes={
                **changes,
                "legal_name": legal or "",
                "social_name": social or "",
                "birth_date": birth_date,
            },
            unknown=unknown,
            reason="",
            source="staff_recorded",
        )
        matching_enrollment_id = None
        if normalized is not None:
            outcome = _append_identifier(
                clinic=clinic,
                patient_id=enrollment.patient_id,
                kind=identifier_kind,
                normalized=normalized,
                issuer=None,
            )
            matching_enrollment_id = outcome.matching_enrollment_id
        return RegistrationOutcome(
            registration=registration,
            matching_enrollment_id=matching_enrollment_id,
        )


def _explicit_answer(key: object, value: object) -> tuple[str, str] | None:
    """Return ``(field, text)`` for an explicit answer to a demographic field."""
    if type(key) is not str or not key.startswith("q_"):
        return None
    field = key[2:]
    if field not in DEMOGRAPHICS_FIELD_VALUES or type(value) is not str:
        return None
    text = value.strip()
    return (field, text) if text else None


def _carried_answers(
    answers: Mapping[object, object],
    current: PatientDemographics | None,
    patient: Patient,
    clinic: Clinic,
) -> tuple[dict[str, object], dict[str, object], tuple[NotCarried, ...]]:
    """Map questionnaire answers to explicit changes and non-answers.

    Only an explicit patient answer carries, and each field carries on its
    own. A blank, whitespace-only, missing or non-text answer means "no
    information supplied" and never touches the record. An answer that is
    exactly ``not_informed`` or ``declined`` (a template's deliberate
    non-answer option) never replaces a recorded value and carries only
    when it says more than the recorded non-answer: a decline is never
    downgraded to "not informed" or erased. An answer equal to the recorded
    value carries nothing, so a no-op never relabels the record as patient
    reported. A malformed answer is skipped and reported as not carried;
    the other answers still carry.
    """
    recorded = _version_values(current, patient)
    statuses = _version_unknown(current)
    changes: dict[str, object] = {}
    unknown: dict[str, object] = {}
    skipped: list[NotCarried] = []
    for key, value in answers.items():
        answer = _explicit_answer(key, value)
        if answer is None:
            continue
        field, text = answer
        if text in UNKNOWN_DEMOGRAPHIC_VALUES:
            _carry_non_answer(field, text, recorded, statuses, changes, unknown)
            continue
        try:
            cleaned = _clean_field(field, text, clinic)
        except DemographicsInputError:
            skipped.append(NotCarried(field=field, reason="invalid_value"))
            continue
        if cleaned != recorded[field]:
            changes[field] = cleaned
    return changes, unknown, tuple(skipped)


def _carry_non_answer(  # noqa: PLR0913 - one field against the whole record
    field: str,
    text: str,
    recorded: Mapping[str, FieldValue],
    statuses: Mapping[str, str],
    changes: dict[str, object],
    unknown: dict[str, object],
) -> None:
    value = recorded[field]
    if value not in (None, *UNKNOWN_DEMOGRAPHIC_VALUES):
        return
    held = value if field in CODED_FIELDS else statuses.get(field)
    if _NON_ANSWER_RANK[text] <= _NON_ANSWER_RANK.get(
        held if isinstance(held, str) else None, 0
    ):
        return
    if field in CODED_FIELDS:
        changes[field] = text
    else:
        unknown[field] = text


def carry_forward_demographics(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    reason: str,
) -> CarryForwardOutcome:
    """Carry explicit questionnaire answers into demographics, unverified.

    Answers whose keys name demographic fields (``q_<field>``) carry into a
    new version recorded with ``source='patient_reported'``, so staff
    surfaces present it as reported by the patient and unverified. Blank or
    missing answers supply no information and never erase what staff
    recorded, and a malformed answer is skipped on its own (see
    ``_carried_answers``). ``version`` is ``None`` when nothing new carried;
    ``not_carried`` names each skipped field and why.
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
        if response is None or not isinstance(response.answers, dict):
            return CarryForwardOutcome(version=None, not_carried=())
        current = _latest_demographics(clinic.organization_id, enrollment.patient_id)
        changes, unknown, skipped = _carried_answers(
            response.answers, current, enrollment.patient, clinic
        )
        if not changes and not unknown:
            return CarryForwardOutcome(version=None, not_carried=skipped)
        version = _apply_demographics(
            clinic=clinic,
            enrollment=enrollment,
            actor_id=actor_id,
            actor_label=_actor_label(),
            expected_version=current.version if current is not None else 0,
            changes=changes,
            unknown=unknown,
            reason=" ".join(reason.split()),
            source="patient_reported",
        )
        return CarryForwardOutcome(version=version, not_carried=skipped)


def _current_identifiers(organization_id: UUID) -> QuerySet[PatientIdentifier]:
    """Active identifiers that are the latest version of their kind."""
    newer = PatientIdentifier.objects.filter(
        organization_id=OuterRef("organization_id"),
        patient_id=OuterRef("patient_id"),
        kind=OuterRef("kind"),
        version__gt=OuterRef("version"),
    )
    return PatientIdentifier.objects.filter(
        organization_id=organization_id,
        retired_at__isnull=True,
    ).filter(~Exists(newer))


def _latest_identifier(
    organization_id: UUID, patient_id: UUID, kind: str
) -> PatientIdentifier | None:
    return (
        PatientIdentifier.objects.filter(
            organization_id=organization_id, patient_id=patient_id, kind=kind
        )
        .order_by("-version")
        .first()
    )


def _append_identifier(
    *,
    clinic: Clinic,
    patient_id: UUID,
    kind: str,
    normalized: str,
    issuer: str | None,
) -> IdentifierOutcome:
    acquire_advisory_locks((patient_lock_key(clinic.organization_id, patient_id),))
    digests = _identifier_digests(kind, normalized)
    digest, key_version = digests[-1]
    latest = _latest_identifier(clinic.organization_id, patient_id, kind)
    if latest is not None and latest.retired_at is None:
        raise IdentifierConflictError
    duplicate = (
        _current_identifiers(clinic.organization_id)
        .filter(kind=kind, blind_index__in=[candidate for candidate, _ in digests])
        .exclude(patient_id=patient_id)
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
    identifier = PatientIdentifier.objects.create(
        organization_id=clinic.organization_id,
        patient_id=patient_id,
        clinic_id=clinic.pk,
        kind=kind,
        value=normalized,
        blind_index=digest,
        index_key_version=key_version,
        issuer=issuer,
        version=(latest.version + 1) if latest is not None else 1,
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
    tenant-keyed blind index per kind, as the next version of that kind.
    When another patient in the same organization already holds the same
    active identifier the record is still written and the outcome names the
    earliest enrollment of the matching patient so review can adjudicate
    (todo 18).
    """
    normalized = normalize_identifier(kind, value)
    issuer_text = _clean_text(issuer)
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.write"
        )
        return _append_identifier(
            clinic=clinic,
            patient_id=enrollment.patient_id,
            kind=kind,
            normalized=normalized,
            issuer=issuer_text,
        )


def retire_identifier(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    kind: str,
    expected_version: int,
) -> PatientIdentifier:
    """Retire one active identifier by appending a retirement version."""
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
        latest = _latest_identifier(clinic.organization_id, enrollment.patient_id, kind)
        if latest is None or latest.retired_at is not None:
            raise DemographicsInputError
        if latest.version != expected_version:
            raise DemographicsStaleError
        retired = PatientIdentifier.objects.create(
            organization_id=clinic.organization_id,
            patient_id=enrollment.patient_id,
            clinic_id=clinic.pk,
            kind=kind,
            value=latest.value,
            blind_index=latest.blind_index,
            index_key_version=latest.index_key_version,
            issuer=latest.issuer or None,
            version=latest.version + 1,
            retired_at=timezone.now(),
        )
        record_phase1_event(
            "intake.identifier.retired",
            clinic_id=clinic.pk,
            affected_record_id=retired.pk,
        )
        return retired


def _identifier_matches(
    organization_id: UUID,
    kind: str,
    normalized: str,
) -> list[UUID]:
    digests = _identifier_digests(kind, normalized)
    return list(
        _current_identifiers(organization_id)
        .filter(kind=kind, blind_index__in=[digest for digest, _ in digests])
        .values_list("patient_id", flat=True)
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
    different DEK. Result shape reuses the registry page contract, and each
    row shows the patient's current (latest-version) name.
    """
    normalized = normalize_identifier(kind, value)
    with transaction.atomic():
        try:
            require_permission("demographics.read", clinic_id=clinic_id)
            clinic = Clinic.objects.get(pk=clinic_id)
        except (CurrentActorError, Clinic.DoesNotExist) as error:
            raise PatientAccessDeniedError from error
        patient_ids = _identifier_matches(clinic.organization_id, kind, normalized)
        enrollments = list(
            PatientClinicEnrollment.objects.select_related("patient")
            .filter(
                organization_id=clinic.organization_id,
                clinic_id=clinic.pk,
                patient_id__in=patient_ids,
            )
            .order_by("created_at", "id")[:100]
        )
        # Ascending versions: the latest version of each patient wins.
        latest: dict[UUID, PatientDemographics] = {}
        for row in PatientDemographics.objects.filter(
            organization_id=clinic.organization_id,
            patient_id__in=[enrollment.patient_id for enrollment in enrollments],
        ).order_by("version"):
            latest[row.patient_id] = row
        items = tuple(
            PatientSearchItem(
                enrollment_id=enrollment.pk,
                full_name=enrollment.patient.full_name,
                birth_date=enrollment.patient.birth_date,
                display_name=_display_name(
                    enrollment.patient, latest.get(enrollment.patient_id)
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


def _latest_by_key[
    RowT: (
        PatientIdentifier,
        PatientAddress,
        EmergencyContact,
        InsuranceMembership,
    )
](rows: QuerySet[RowT], key_field: str) -> dict[object, RowT]:
    """Keep the highest version per kind/slot (rows ordered by -version)."""
    latest: dict[object, RowT] = {}
    for row in rows.order_by("-version"):
        latest.setdefault(getattr(row, key_field), row)
    return latest


def _identifier_views(
    organization_id: UUID, patient_id: UUID
) -> tuple[IdentifierView, ...]:
    latest = _latest_by_key(
        PatientIdentifier.objects.filter(
            organization_id=organization_id, patient_id=patient_id
        ),
        "kind",
    )
    return tuple(
        IdentifierView(
            kind=row.kind,
            masked_value=_mask_identifier(row.kind, row.value),
            issuer=row.issuer or "",
            version=row.version,
            retired=row.retired_at is not None,
        )
        for _, row in sorted(latest.items(), key=lambda item: str(item[0]))
    )


def _membership_views(
    organization_id: UUID, patient_id: UUID
) -> tuple[MembershipView, ...]:
    latest = _latest_by_key(
        InsuranceMembership.objects.filter(
            organization_id=organization_id, patient_id=patient_id
        ),
        "sequence",
    )
    return tuple(
        MembershipView(
            sequence=row.sequence,
            payer_name=row.payer_name or "",
            ans_number=row.ans_number or "",
            membership_number=row.membership_number or "",
            plan_name=row.plan_name or "",
            valid_until=row.valid_until,
            version=row.version,
        )
        for _, row in sorted(latest.items(), key=lambda item: cast("int", item[0]))
        if row.retired_at is None
    )


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
        addresses = _latest_by_key(
            PatientAddress.objects.filter(
                organization_id=organization_id, patient_id=patient_id
            ),
            "kind",
        )
        contacts = _latest_by_key(
            EmergencyContact.objects.filter(
                organization_id=organization_id, patient_id=patient_id
            ),
            "sequence",
        )
        memberships = _membership_views(organization_id, patient_id)
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
            .order_by("-demographics__version")[:_CORRECTION_HISTORY_LIMIT]
        )
        record_phase1_event(
            "intake.demographics.viewed",
            clinic_id=clinic_id,
            affected_record_id=enrollment.pk,
        )
        values = _version_values(current, enrollment.patient)
        active_addresses = {
            kind: row for kind, row in addresses.items() if row.retired_at is None
        }
        active_contacts = {
            slot: row for slot, row in contacts.items() if row.retired_at is None
        }
        membership_slots = {view.sequence for view in memberships}
        return DemographicsProfile(
            enrollment_id=enrollment.pk,
            patient_id=patient_id,
            display_name=_display_name(enrollment.patient, current),
            version=current.version if current is not None else 0,
            values={
                field: value.isoformat() if isinstance(value, date) else value or ""
                for field, value in values.items()
            },
            unknown=_version_unknown(current),
            birth_date=cast("date | None", values["birth_date"]),
            source=current.source if current is not None else "",
            required_fields=_required_fields(clinic.pk),
            identifiers=_identifier_views(organization_id, patient_id),
            addresses=tuple(
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
                for kind, row in sorted(active_addresses.items())
            ),
            emergency_contacts=tuple(
                EmergencyContactView(
                    sequence=row.sequence,
                    name=row.name or "",
                    relationship=row.relationship or "",
                    phone=row.phone or "",
                    version=row.version,
                )
                for _, row in sorted(active_contacts.items())
            ),
            memberships=memberships,
            free_address_kinds=tuple(
                kind for kind in ADDRESS_KIND_VALUES if kind not in active_addresses
            ),
            free_contact_slots=tuple(
                slot for slot in SECTION_SLOTS if slot not in active_contacts
            ),
            free_membership_slots=tuple(
                slot for slot in SECTION_SLOTS if slot not in membership_slots
            ),
            corrections=corrections,
        )


@dataclass(frozen=True, slots=True)
class BillingDemographics:
    """The minimum identity a finance role may resolve for one enrollment."""

    enrollment_id: UUID
    patient_id: UUID
    display_name: str
    birth_date: date | None
    identifiers: tuple[IdentifierView, ...]
    memberships: tuple[MembershipView, ...]


def billing_demographics(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
) -> BillingDemographics:
    """Return the narrow read a finance role needs for billing identity.

    Binds ``demographics.billing_read`` (finance and no wider staff role):
    the enrolled patient's display name, birth date, current identifiers
    and payer memberships; the rest of the demographics record stays
    outside this surface.
    """
    with transaction.atomic():
        _, clinic, enrollment = authorized_enrollment_for(
            clinic_id, enrollment_id, "demographics.billing_read"
        )
        organization_id = clinic.organization_id
        patient_id = enrollment.patient_id
        current = _latest_demographics(organization_id, patient_id)
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
            identifiers=tuple(
                view
                for view in _identifier_views(organization_id, patient_id)
                if not view.retired
            ),
            memberships=_membership_views(organization_id, patient_id),
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
    key: tuple[str, object],
    values: dict[str, object] | None,
    clinic: Clinic,
    patient_id: UUID,
    expected_version: int,
    audit_type: str,
) -> _SECTION_MODELS:
    """Append the next version of one kind/slot; ``None`` retires it.

    ``expected_version`` is the rendered current version; 0 creates into a
    kind/slot that holds no current row (never used, or retired).
    """
    key_field, key_value = key
    latest = (
        model.objects.filter(
            organization_id=clinic.organization_id,
            patient_id=patient_id,
            **{key_field: key_value},
        )
        .order_by("-version")
        .first()
    )
    active = latest is not None and latest.retired_at is None
    rendered = latest.version if active and latest is not None else 0
    if expected_version != rendered:
        raise DemographicsStaleError
    if values is None and not active:
        raise DemographicsInputError
    try:
        with transaction.atomic():
            row = model.objects.create(
                organization_id=clinic.organization_id,
                patient_id=patient_id,
                version=(latest.version + 1) if latest is not None else 1,
                retired_at=timezone.now() if values is None else None,
                **{key_field: key_value},
                **(values or {}),
            )
    except IntegrityError as error:
        raise DemographicsStaleError from error
    record_phase1_event(
        audit_type
        if values is not None
        else f"{audit_type.removesuffix('saved')}retired",
        clinic_id=clinic.pk,
        affected_record_id=row.pk,
    )
    return row


def _section_enrollment(
    clinic_id: UUID, enrollment_id: UUID
) -> tuple[Clinic, PatientClinicEnrollment]:
    _, clinic, enrollment = authorized_enrollment_for(
        clinic_id, enrollment_id, "demographics.write"
    )
    acquire_advisory_locks(
        (patient_lock_key(clinic.organization_id, enrollment.patient_id),)
    )
    return clinic, enrollment


def _address_values(address: Mapping[str, object]) -> dict[str, object]:
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
    if isinstance(state, str):
        state = state.upper()
        if state not in _UF_VALUES:
            raise DemographicsInputError
        values["state_code"] = state
    if all(value is None for value in values.values()):
        raise DemographicsInputError
    return values


def save_patient_address(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    kind: str,
    expected_version: int,
    address: Mapping[str, object] | None,
) -> PatientAddress:
    """Create, replace or retire the address of one kind.

    ``address`` maps the address fields to strings; ``None`` retires the
    current address. ``expected_version`` is the rendered current version
    (0 to create into an empty kind).
    """
    if kind not in ADDRESS_KIND_VALUES:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    values = None if address is None else _address_values(address)
    with transaction.atomic():
        clinic, enrollment = _section_enrollment(clinic_id, enrollment_id)
        return cast(
            "PatientAddress",
            _save_section(
                model=PatientAddress,
                key=("kind", kind),
                values=values,
                clinic=clinic,
                patient_id=enrollment.patient_id,
                expected_version=expected_version,
                audit_type="intake.address.saved",
            ),
        )


def _contact_values(contact: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(contact, Mapping) or set(contact) - set(_CONTACT_FIELDS):
        raise DemographicsInputError
    values: dict[str, object] = {
        field: _clean_text(contact.get(field)) for field in _CONTACT_FIELDS
    }
    phone = values["phone"]
    if isinstance(phone, str):
        digits = re.sub(r"[\s().\-]", "", phone)
        if _PHONE_PATTERN.fullmatch(digits) is None:
            raise DemographicsInputError
        values["phone"] = digits
    if values["name"] is None and values["phone"] is None:
        raise DemographicsInputError
    return values


def save_emergency_contact(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    sequence: int,
    expected_version: int,
    contact: Mapping[str, object] | None,
) -> EmergencyContact:
    """Create, replace or retire one emergency contact slot (1-3)."""
    if type(sequence) is not int or sequence not in SECTION_SLOTS:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    values = None if contact is None else _contact_values(contact)
    with transaction.atomic():
        clinic, enrollment = _section_enrollment(clinic_id, enrollment_id)
        return cast(
            "EmergencyContact",
            _save_section(
                model=EmergencyContact,
                key=("sequence", sequence),
                values=values,
                clinic=clinic,
                patient_id=enrollment.patient_id,
                expected_version=expected_version,
                audit_type="intake.emergency_contact.saved",
            ),
        )


def _membership_values(membership: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(membership, Mapping) or set(membership) - set(_MEMBERSHIP_FIELDS):
        raise DemographicsInputError
    values: dict[str, object] = {}
    for field in _MEMBERSHIP_FIELDS:
        raw = membership.get(field)
        if field == "valid_until":
            if raw is not None and type(raw) is not date:
                raise DemographicsInputError
            values[field] = raw
        else:
            values[field] = _clean_text(raw)
    if values["payer_name"] is None and values["membership_number"] is None:
        raise DemographicsInputError
    return values


def save_insurance_membership(
    *,
    clinic_id: UUID,
    enrollment_id: UUID,
    sequence: int,
    expected_version: int,
    membership: Mapping[str, object] | None,
) -> InsuranceMembership:
    """Create, replace or retire one insurance membership slot (1-3)."""
    if type(sequence) is not int or sequence not in SECTION_SLOTS:
        raise DemographicsInputError
    if type(expected_version) is not int or expected_version < 0:
        raise DemographicsInputError
    values = None if membership is None else _membership_values(membership)
    with transaction.atomic():
        clinic, enrollment = _section_enrollment(clinic_id, enrollment_id)
        return cast(
            "InsuranceMembership",
            _save_section(
                model=InsuranceMembership,
                key=("sequence", sequence),
                values=values,
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

    ``required_fields`` is a closed list of demographic field names that
    registration and every demographics write must leave answered; an
    explicit ``not_informed``/``declined`` answer satisfies a requirement so
    no invented values are forced. Publishing is clinic configuration.
    """
    fields = tuple(str(field) for field in required_fields)
    if any(field not in DEMOGRAPHICS_FIELD_VALUES for field in fields):
        raise DemographicsInputError
    with transaction.atomic():
        # RP Config/templates: a clinic manager/finance role configures its
        # clinic; the organization admin/owner configures every clinic.
        try:
            try:
                actor_id = require_permission(
                    "configuration.clinic", clinic_id=clinic_id
                )
            except CurrentActorError:
                actor_id = require_permission(
                    "configuration.organization", clinic_id=clinic_id
                )
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

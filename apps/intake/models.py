"""Organization-scoped patient identity, enrollment and contact models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar, Final

from django.conf import settings
from django.contrib.postgres.fields import ArrayField
from django.db import models

from apps.identity.models import Clinic
from apps.tenancy.fields import (
    EncryptedDateField,
    EncryptedJSONField,
    EncryptedPatientNameField,
    EncryptedTextField,
)
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class Patient(TenantScopedModel):
    """Minimal organization-level patient identity.

    ``full_name`` and ``birth_date`` persist only as tenant envelopes; the
    approved registry search runs inside ``clinic_app.patient_registry_*``
    so ciphertext never feeds SQL predicates or ordering. ``full_name`` is
    the registry name staff address the patient by (the legal name, or the
    social name when no legal name was given); ``birth_date`` stays empty
    when it was not informed instead of carrying an invented date. Both
    mirror the latest demographics version and change only in the same
    transaction that appends that version.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    full_name = EncryptedPatientNameField(purpose="intake.patient.full_name")
    birth_date = EncryptedDateField(purpose="intake.patient.birth_date", null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Expose the composite target without natural-person uniqueness."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_patient_org_id_uniq",
            )
        ]


class PatientClinicEnrollment(TenantScopedModel):
    """Bind one organization patient to one authorized clinic."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    idempotency_key = models.UUIDField()
    create_fingerprint = models.BinaryField(max_length=32, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Prevent duplicate enrollments and keys while preserving identity."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "patient"),
                name="intake_enrollment_org_clinic_patient_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="intake_enrollment_org_clinic_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="intake_enrollment_org_idempotency_uniq",
            ),
        ]


CONTACT_CHANNEL_VALUES: Final = ["sms", "email", "whatsapp"]
CONTACT_PURPOSE_VALUES: Final = [
    "appointment_reminder",
    "booking_confirmation",
    "waitlist_offer",
]
CONTACT_EVENT_TYPE_VALUES: Final = [
    "contact_saved",
    "contact_verified",
    "verification_invalidated",
    "preference_opted_in",
    "preference_opted_out",
]
CONTACT_EVENT_TYPES: Final = CONTACT_EVENT_TYPE_VALUES[:3]
PREFERENCE_EVENT_TYPES: Final = CONTACT_EVENT_TYPE_VALUES[3:]


class PatientContact(TenantScopedModel):
    """One destination a patient may be reached at through one channel.

    Verification binds to the exact destination: ``destination_version``
    increments on every destination change while ``verified_version`` keeps
    the version that was verified, so a changed destination is unverified
    until staff confirm it again. Verification never implies identity and a
    contact row never implies consent to automated messages.
    """

    class Channel(models.TextChoices):
        """Outbound channels matching the comms operation vocabulary."""

        SMS = "sms", "SMS"
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    channel = models.CharField(max_length=32, choices=Channel.choices)
    destination = EncryptedTextField(purpose="intake.patientcontact.destination")
    destination_version = models.PositiveIntegerField(default=1)
    verified_version = models.PositiveIntegerField(default=0)
    verified_at = models.DateTimeField(null=True, blank=True)
    verification_method = models.CharField(max_length=64, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep one destination per patient channel with coherent versions."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "patient", "channel"),
                name="intake_contact_org_patient_channel_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_contact_org_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=CONTACT_CHANNEL_VALUES),
                name="intake_contact_channel_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    verified_version__lte=models.F("destination_version")
                ),
                name="intake_contact_version_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(verified_version__gt=0)
                    & models.Q(verified_at__isnull=False)
                    & ~models.Q(verification_method="")
                )
                | (
                    models.Q(verified_version=0)
                    & models.Q(verified_at__isnull=True)
                    & models.Q(verification_method="")
                ),
                name="intake_contact_verification_check",
            ),
        ]

    @property
    def is_verified(self) -> bool:
        """Report whether the current destination version was verified."""
        return self.verified_version == self.destination_version


class PatientChannelPreference(TenantScopedModel):
    """One versioned opt-in state for a purpose and channel in a clinic.

    A missing row means no permission: automated messages require an
    explicit opted-in row, and a preference never records a processing
    legal basis or patient identity.
    """

    class Purpose(models.TextChoices):
        """Automated-message purposes a patient may allow per channel."""

        APPOINTMENT_REMINDER = "appointment_reminder", "Appointment reminder"
        BOOKING_CONFIRMATION = "booking_confirmation", "Booking confirmation"
        WAITLIST_OFFER = "waitlist_offer", "Oferta da lista de espera"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    purpose = models.CharField(max_length=32, choices=Purpose.choices)
    channel = models.CharField(max_length=32, choices=PatientContact.Channel.choices)
    opted_in = models.BooleanField(default=False)
    version = models.PositiveIntegerField(default=1)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep one versioned state per clinic, purpose and channel."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "patient", "purpose", "channel"),
                name="intake_preference_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="intake_preference_org_clinic_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(purpose__in=CONTACT_PURPOSE_VALUES),
                name="intake_preference_purpose_check",
            ),
            models.CheckConstraint(
                condition=models.Q(channel__in=CONTACT_CHANNEL_VALUES),
                name="intake_preference_channel_check",
            ),
        ]


DEMOGRAPHICS_SOURCE_VALUES: Final = [
    "staff_recorded",
    "patient_reported",
]
SEX_AT_BIRTH_VALUES: Final = [
    "declined",
    "female",
    "intersex",
    "male",
    "not_informed",
]
GENDER_IDENTITY_VALUES: Final = [
    "declined",
    "man",
    "non_binary",
    "not_informed",
    "other",
    "woman",
]
IDENTIFIER_KIND_VALUES: Final = [
    "cpf",
    "cns",
    "passport",
    "rg",
    "other",
]
ADDRESS_KIND_VALUES: Final = ["home", "other", "work"]
# Deliberate non-answers are recorded, never invented: the coded fields carry
# them in their vocabulary, and every other field records them in the
# version's ``unknown_fields`` map (field name -> one of these codes).
UNKNOWN_DEMOGRAPHIC_VALUES: Final = ("not_informed", "declined")


class PatientDemographics(TenantScopedModel):
    """One immutable demographics version for an organization patient.

    A demographics row is append-only: every accepted correction inserts the
    next ``version`` and a matching ``DemographicsCorrection`` receipt, so the
    update surface needs no UPDATE grant and history is never rewritten.
    ``source`` records who supplied the version; questionnaire carry-forward
    writes ``patient_reported`` versions that staff review as unverified.
    ``unknown_fields`` maps a field left empty on purpose to
    ``not_informed``/``declined`` so a deliberate non-answer is recorded
    instead of forcing an invented value.
    """

    class Source(models.TextChoices):
        """Provenance of one recorded version."""

        STAFF_RECORDED = "staff_recorded", "Staff recorded"
        PATIENT_REPORTED = "patient_reported", "Reported by patient"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(PatientClinicEnrollment, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    # Normalized by the service; empty when the legal name was not informed.
    legal_name = EncryptedTextField(
        purpose="intake.patientdemographics.legal_name", null=True
    )
    social_name = EncryptedTextField(
        purpose="intake.patientdemographics.social_name", null=True
    )
    preferred_name = EncryptedTextField(
        purpose="intake.patientdemographics.preferred_name", null=True
    )
    sex_at_birth = EncryptedTextField(
        purpose="intake.patientdemographics.sex_at_birth", null=True
    )
    gender_identity = EncryptedTextField(
        purpose="intake.patientdemographics.gender_identity", null=True
    )
    pronouns = EncryptedTextField(
        purpose="intake.patientdemographics.pronouns", null=True
    )
    language = EncryptedTextField(
        purpose="intake.patientdemographics.language", null=True
    )
    accessibility_needs = EncryptedTextField(
        purpose="intake.patientdemographics.accessibility_needs", null=True
    )
    occupation = EncryptedTextField(
        purpose="intake.patientdemographics.occupation", null=True
    )
    birth_date = EncryptedDateField(
        purpose="intake.patientdemographics.birth_date", null=True
    )
    unknown_fields = EncryptedJSONField(
        purpose="intake.patientdemographics.unknown_fields", null=True
    )
    source = models.CharField(
        max_length=32,
        choices=Source.choices,
        default=Source.STAFF_RECORDED,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep versions append-only, dense per patient and org-scoped."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_demographics_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "patient", "version"),
                name="intake_demographics_org_patient_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_demographics_version_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(source__in=DEMOGRAPHICS_SOURCE_VALUES),
                name="intake_demographics_source_check",
            ),
        ]


class DemographicsCorrection(TenantScopedModel):
    """Append-only receipt binding one demographics version to its correction.

    ``demographics`` is the version the correction produced; ``previous`` is
    the version it superseded (empty for the first record). The prior field
    values live only in the superseded version row, so history never carries
    a plaintext shadow copy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    demographics = models.ForeignKey(PatientDemographics, on_delete=models.PROTECT)
    previous = models.ForeignKey(
        PatientDemographics,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="superseded_by",
    )
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    actor_label = models.CharField(max_length=150)
    reason = EncryptedTextField(
        purpose="intake.demographicscorrection.reason", null=True
    )
    changed_fields = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One correction receipt per produced version, org-scoped."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_demographics_correction_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "demographics"),
                name="intake_demographics_correction_version_uniq",
            ),
            models.CheckConstraint(
                condition=(
                    models.expressions.RawSQL(
                        "jsonb_typeof(changed_fields) = 'array'",
                        (),
                        output_field=models.BooleanField(),
                    )
                ),
                name="intake_demographics_correction_fields_check",
            ),
        ]


class PatientIdentifier(TenantScopedModel):
    """One immutable identifier version of a kind for an organization patient.

    ``value`` is a tenant envelope; ``blind_index`` is a tenant-keyed HMAC of
    the normalized value that powers exact-match lookup without feeding
    ciphertext or plaintext into SQL predicates. Two patients may hold the
    same identifier: services surface a ``possible_duplicate`` review hint
    instead of silently blocking registration. Rows are append-only: the
    current identifier of a kind is its highest ``version``; a version with
    ``retired_at`` set retires it, so every replaced value stays on record.
    """

    class Kind(models.TextChoices):
        """Identifier kinds accepted at registration."""

        CPF = "cpf", "CPF"
        CNS = "cns", "CNS"
        RG = "rg", "RG"
        PASSPORT = "passport", "Passport"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    value = EncryptedTextField(purpose="intake.patientidentifier.value")
    blind_index = models.BinaryField(max_length=32, editable=False)
    index_key_version = models.PositiveIntegerField(default=1)
    issuer = EncryptedTextField(purpose="intake.patientidentifier.issuer", null=True)
    version = models.PositiveIntegerField(default=1)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep identifier versions dense per patient and kind."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_identifier_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "patient", "kind", "version"),
                name="intake_identifier_org_patient_kind_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=IDENTIFIER_KIND_VALUES),
                name="intake_identifier_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_identifier_version_positive",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "pg_catalog.octet_length(blind_index) = 32",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="intake_identifier_blind_index_32_check",
            ),
        ]


class PatientAddress(TenantScopedModel):
    """One immutable address version per kind for an organization patient.

    The current address of a kind is its highest ``version``; a version with
    ``retired_at`` set removes it while earlier versions stay on record.
    """

    class Kind(models.TextChoices):
        """Address kinds a clinic may record."""

        HOME = "home", "Home"
        WORK = "work", "Work"
        OTHER = "other", "Other"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind.choices)
    postal_code = EncryptedTextField(
        purpose="intake.patientaddress.postal_code", null=True
    )
    street = EncryptedTextField(purpose="intake.patientaddress.street", null=True)
    street_number = EncryptedTextField(
        purpose="intake.patientaddress.street_number", null=True
    )
    complement = EncryptedTextField(
        purpose="intake.patientaddress.complement", null=True
    )
    district = EncryptedTextField(purpose="intake.patientaddress.district", null=True)
    city = EncryptedTextField(purpose="intake.patientaddress.city", null=True)
    state_code = EncryptedTextField(
        purpose="intake.patientaddress.state_code", null=True
    )
    version = models.PositiveIntegerField(default=1)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep address versions dense per patient and kind."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_address_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "patient", "kind", "version"),
                name="intake_address_org_patient_kind_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=ADDRESS_KIND_VALUES),
                name="intake_address_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_address_version_positive",
            ),
        ]


class EmergencyContact(TenantScopedModel):
    """One immutable emergency contact slot version for a patient.

    The current contact of a slot is its highest ``version``; a version with
    ``retired_at`` set empties the slot while earlier versions stay on record.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    sequence = models.PositiveSmallIntegerField(default=1)
    name = EncryptedTextField(purpose="intake.emergencycontact.name", null=True)
    relationship = EncryptedTextField(
        purpose="intake.emergencycontact.relationship", null=True
    )
    phone = EncryptedTextField(purpose="intake.emergencycontact.phone", null=True)
    version = models.PositiveIntegerField(default=1)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep contact slots bounded and their versions dense."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_emergency_contact_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "patient", "sequence", "version"),
                name="intake_emergency_contact_org_patient_seq_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1) & models.Q(sequence__lte=3),
                name="intake_emergency_contact_sequence_check",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_emergency_contact_version_positive",
            ),
        ]


class InsuranceMembership(TenantScopedModel):
    """One payer membership slot for an organization patient.

    Payer identity is encrypted free text until todo 59 introduces the
    ``insurance.Payer`` table and a nullable foreign key plus matcher; no
    plaintext shadow column is kept for it. Versions are append-only like
    the other identity sections.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    sequence = models.PositiveSmallIntegerField(default=1)
    payer_name = EncryptedTextField(
        purpose="intake.insurancemembership.payer_name", null=True
    )
    ans_number = EncryptedTextField(
        purpose="intake.insurancemembership.ans_number", null=True
    )
    membership_number = EncryptedTextField(
        purpose="intake.insurancemembership.membership_number", null=True
    )
    plan_name = EncryptedTextField(
        purpose="intake.insurancemembership.plan_name", null=True
    )
    valid_until = EncryptedDateField(
        purpose="intake.insurancemembership.valid_until", null=True
    )
    version = models.PositiveIntegerField(default=1)
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep membership slots bounded and their versions dense."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_membership_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "patient", "sequence", "version"),
                name="intake_membership_org_patient_seq_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(sequence__gte=1) & models.Q(sequence__lte=3),
                name="intake_membership_sequence_check",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_membership_version_positive",
            ),
        ]


class ClinicIntakePolicy(TenantScopedModel):
    """Append-only clinic policy marking required demographics fields.

    The current policy is the latest ``version``; ``required_fields`` is a
    JSON array of demographic field names that registration and
    ``update_demographics`` refuse to leave unanswered. Fields absent from
    the list stay optional, and an explicit ``not_informed``/``declined``
    answer always satisfies a requirement without inventing values.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    required_fields = models.JSONField(default=list)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep versions monotonic and field names closed."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_policy_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "version"),
                name="intake_policy_org_clinic_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_policy_version_positive",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "jsonb_typeof(required_fields) = 'array'",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="intake_policy_fields_check",
            ),
        ]


PATIENT_OPERATION_VALUES: Final = [
    "enrollment_view",
    "questionnaires",
    "booking",
    "records",
    "consent",
    "teleconsult",
    "billing",
]


class PatientAccessGrant(TenantScopedModel):
    """One staff-issued single-use invitation bound to an enrollment.

    Only the SHA-256 hash of the random secret is stored; the raw code is
    shown once at issuance and never persisted. The grant expires 24 hours
    after creation, is consumed by its first successful redemption, and is
    invalidated together with every session minted from it when staff
    revoke it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(PatientClinicEnrollment, on_delete=models.PROTECT)
    issued_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    issued_by_label = models.CharField(max_length=150)
    secret_hash = models.BinaryField(max_length=32, editable=False)
    operations = ArrayField(models.CharField(max_length=32), size=None)
    expires_at = models.DateTimeField()
    consumed_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep one hash per secret and a coherent lifecycle."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_grant_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="intake_grant_org_clinic_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("secret_hash",),
                name="intake_grant_secret_hash_uniq",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "operations::pg_catalog.text[] <@ %s::text[] AND "
                    "pg_catalog.array_length(operations, 1) > 0",
                    (PATIENT_OPERATION_VALUES,),
                    output_field=models.BooleanField(),
                ),
                name="intake_grant_operations_check",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="intake_grant_expiry_check",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "pg_catalog.octet_length(secret_hash) = 32",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="intake_grant_secret_hash_32_check",
            ),
        ]


class PatientSession(TenantScopedModel):
    """One short-lived revocable session minted by a redeemed invitation.

    The row is the server-side binding: organization, clinic, patient,
    enrollment and the allowed operation set are fixed at redemption and
    never taken from the request. ``expires_at`` is the absolute 8-hour
    deadline; ``idle_expires_at`` is the rolling 30-minute deadline renewed
    by the database touch function on each validated request.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    grant = models.ForeignKey(PatientAccessGrant, on_delete=models.PROTECT)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(PatientClinicEnrollment, on_delete=models.PROTECT)
    operations = ArrayField(models.CharField(max_length=32), size=None)
    expires_at = models.DateTimeField()
    idle_expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep the session bound to one grant and one enrollment."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_session_org_id_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="intake_session_org_clinic_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "operations::pg_catalog.text[] <@ %s::text[] AND "
                    "pg_catalog.array_length(operations, 1) > 0",
                    (PATIENT_OPERATION_VALUES,),
                    output_field=models.BooleanField(),
                ),
                name="intake_session_operations_check",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="intake_session_expiry_check",
            ),
        ]


class QuestionnaireTemplate(TenantScopedModel):
    """Immutable clinic configuration; each revision is a separate row."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    key = models.SlugField(max_length=64)
    version = models.PositiveIntegerField()
    title = models.CharField(max_length=160)
    questions = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Identify each exact version without replacing historical definitions."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "key", "version"),
                name="intake_questionnaire_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="intake_questionnaire_version_positive",
            ),
        ]


class QuestionnaireResponse(TenantScopedModel):
    """Enrollment-bound answers to one immutable version, with optimistic locking."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(PatientClinicEnrollment, on_delete=models.PROTECT)
    template = models.ForeignKey(QuestionnaireTemplate, on_delete=models.PROTECT)
    appointment = models.ForeignKey(
        "scheduling.Appointment", on_delete=models.PROTECT, null=True, blank=True
    )
    answers = EncryptedJSONField(
        purpose="intake.questionnaireresponse.answers", null=True, empty={}
    )
    state = models.CharField(max_length=16, default="draft")
    revision = models.PositiveIntegerField(default=1)
    submitted_at = models.DateTimeField(null=True, blank=True)
    reopen_reason = EncryptedTextField(
        purpose="intake.questionnaireresponse.reopen_reason", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep state and submission timestamp coherent."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    models.Q(state="draft", submitted_at__isnull=True)
                    | models.Q(state="submitted", submitted_at__isnull=False)
                ),
                name="intake_response_state_check",
            ),
        ]


class QuestionnaireEvent(TenantScopedModel):
    """Database-written append-only transition receipt with exact answer history."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    response = models.ForeignKey(QuestionnaireResponse, on_delete=models.PROTECT)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    actor_id = models.UUIDField(null=True)
    patient_session_id = models.UUIDField(null=True)
    action = models.CharField(max_length=16)
    revision = models.PositiveIntegerField()
    # The receipt trigger copies the response's ciphertext verbatim, so the
    # event columns keep the source purposes: envelopes are purpose-bound and
    # a trigger cannot re-encrypt without key material.
    answers = EncryptedJSONField(
        purpose="intake.questionnaireresponse.answers", null=True, empty={}
    )
    reason = EncryptedTextField(
        purpose="intake.questionnaireresponse.reopen_reason", null=True
    )
    created_at = models.DateTimeField(auto_now_add=True)


class PatientContactEvent(TenantScopedModel):
    """Append-only contact and preference history for one clinic.

    Events carry the actor, the subject row and the resulting version; the
    destination itself is never copied here, so history cannot leak an old
    destination outside the edit screen.
    """

    class EventType(models.TextChoices):
        """Recorded contact and preference transitions."""

        CONTACT_SAVED = "contact_saved", "Contact saved"
        CONTACT_VERIFIED = "contact_verified", "Contact verified"
        VERIFICATION_INVALIDATED = (
            "verification_invalidated",
            "Verification invalidated",
        )
        PREFERENCE_OPTED_IN = "preference_opted_in", "Preference opted in"
        PREFERENCE_OPTED_OUT = "preference_opted_out", "Preference opted out"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient = models.ForeignKey(Patient, on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    actor_label = models.CharField(max_length=150)
    event_type = models.CharField(max_length=32, choices=EventType.choices)
    contact = models.ForeignKey(
        PatientContact,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    preference = models.ForeignKey(
        PatientChannelPreference,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep one exact subject per event."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="intake_contact_event_org_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(event_type__in=CONTACT_EVENT_TYPE_VALUES),
                name="intake_contact_event_type_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(contact__isnull=False)
                    & models.Q(preference__isnull=True)
                    & models.Q(event_type__in=CONTACT_EVENT_TYPES)
                )
                | (
                    models.Q(contact__isnull=True)
                    & models.Q(preference__isnull=False)
                    & models.Q(event_type__in=PREFERENCE_EVENT_TYPES)
                ),
                name="intake_contact_event_subject_check",
            ),
        ]

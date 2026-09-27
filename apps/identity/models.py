"""Identity domain database models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models
from django.utils.translation import gettext_lazy as _

from apps.scheduling.timezones import IanaTimezoneField, validate_iana_timezone
from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class User(AbstractUser):
    """Project authentication user with a UUID primary key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    def _has_role(self, role: UserClinicRole.Role) -> bool:
        return UserClinicRole.objects.filter(user_id=self.pk, role=role).exists()

    @property
    def is_physician_anywhere(self) -> bool:
        """Report a physician assignment visible in the current tenant."""
        return self._has_role(UserClinicRole.Role.PHYSICIAN)

    @property
    def is_clinic_admin_anywhere(self) -> bool:
        """Report an administrator assignment visible in the current tenant."""
        return UserClinicRole.objects.filter(
            user_id=self.pk,
            role__in=(
                UserClinicRole.Role.OWNER,
                UserClinicRole.Role.CLINIC_ADMIN,
            ),
        ).exists()


class Organization(models.Model):
    """Root tenant organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    cnpj = models.CharField(max_length=14)

    def __str__(self) -> str:
        """Return the organization name."""
        return self.name


class Clinic(models.Model):
    """Clinic owned by one organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    name = models.CharField(max_length=255)
    crm_uf = models.CharField(max_length=2)
    timezone = IanaTimezoneField(max_length=64, validators=[validate_iana_timezone])

    class Meta:
        """Define the composite foreign-key target."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="identity_clinic_org_id_uniq",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "btrim(timezone) <> ''",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="identity_clinic_timezone_nonblank",
            ),
        ]

    def __str__(self) -> str:
        """Return the clinic name."""
        return self.name


class ClinicConfiguration(models.Model):
    """Append-only clinic presentation and future-booking configuration."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    display_name = models.CharField(max_length=120)
    contact_email = models.EmailField(blank=True)
    contact_phone = models.CharField(max_length=20, blank=True)
    brand_token = models.CharField(
        max_length=8, choices=(("navy", "Azul"), ("teal", "Verde")), default="navy"
    )
    reminder_hours = models.PositiveSmallIntegerField(default=24)
    queue_quotas = models.JSONField(default=dict, blank=True)
    logo_png = models.BinaryField(default=bytes, blank=True)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Constrain the closed vocabulary and monotonically named versions."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "version"), name="identity_config_version_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1)
                & models.Q(reminder_hours__in=(1, 2, 6, 12, 24, 48, 72))
                & models.Q(brand_token__in=("navy", "teal")),
                name="identity_config_bounded_values",
            ),
            models.CheckConstraint(
                condition=models.expressions.RawSQL(
                    "jsonb_typeof(queue_quotas) = 'object' "
                    "AND queue_quotas - ARRAY['clinic-integrations','clinical',"
                    "'ai-interactive','ai-batch','messaging','finance','bulk']"
                    " = '{}'::jsonb "
                    "AND queue_quotas::text ~ "
                    '\'^\\{("[a-z-]+": [0-9]+(, "[a-z-]+": [0-9]+)*)?\\}$\' '
                    "AND NOT jsonb_path_exists(queue_quotas, "
                    '\'strict $.* ? (@.type() != "number" '
                    "|| @ < 1 || @ > 100000)')",
                    (),
                    output_field=models.BooleanField(),
                ),
                name="identity_config_queue_quotas_shape",
            ),
        ]

    def __str__(self) -> str:
        """Return the stable snapshot identifier, not clinic contact data."""
        return str(self.pk)


class UserClinicRole(models.Model):
    """Canonical clinic membership role assignment."""

    class Role(models.TextChoices):
        """Stored role values."""

        OWNER = "owner", "Owner"
        PHYSICIAN = "physician", "Physician"
        RECEPTIONIST = "receptionist", "Receptionist"
        CLINIC_ADMIN = "clinic_admin", "Clinic admin"
        NURSE = "nurse", "Nurse"
        ALLIED_PROFESSIONAL = "allied_professional", "Allied professional"
        SCHEDULER = "scheduler", "Scheduler"
        CLINIC_MANAGER = "clinic_manager", "Clinic manager"
        FINANCE = "finance", "Finance"
        ORG_ADMIN = "org_admin", "Organization admin"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices)

    class Meta:
        """Prevent duplicate exact role assignments."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "user", "role"),
                name="identity_userclinicrole_assignment_uniq",
            )
        ]

    def __str__(self) -> str:
        """Return stable identifiers and the stored role value."""
        return f"{self.user_id}:{self.clinic_id}:{self.role}"


class UserPreference(models.Model):
    """Per-user display preferences; each user reads and edits only their row.

    Row security binds the row to ``app.current_user_id``; the runtime role may
    insert and update theme/density but never delete. Defaults are light and
    comfortable, so a missing row is a valid state.
    """

    class Theme(models.TextChoices):
        """Stored theme values."""

        LIGHT = "light", _("Light")
        DARK = "dark", _("Dark")

    class Density(models.TextChoices):
        """Stored density values."""

        COMFORTABLE = "comfortable", _("Comfortable")
        COMPACT = "compact", _("Compact")

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, primary_key=True
    )
    theme = models.CharField(max_length=16, choices=Theme, default=Theme.LIGHT)
    density = models.CharField(
        max_length=16, choices=Density, default=Density.COMFORTABLE
    )
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep the stored vocabulary closed at the database."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(theme__in=("light", "dark"))
                & models.Q(density__in=("comfortable", "compact")),
                name="identity_userpreference_closed_values",
            ),
        ]

    def __str__(self) -> str:
        """Return only the stable owner identifier."""
        return str(self.pk)


class PhysicianProfile(models.Model):
    """Owner-provisioned professional identity; never a staff role assignment."""

    class Status(models.TextChoices):
        """Normalized registry states; only regular can satisfy verification."""

        REGULAR = "regular", "Regular"
        EXPIRED = "expired", "Expired"
        REVOKED = "revoked", "Revoked"
        SUSPENDED = "suspended", "Suspended"
        UNKNOWN = "unknown", "Unknown"
        UNAVAILABLE = "unavailable", "Unavailable"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    jurisdiction = models.CharField(max_length=2)
    registration_number = models.CharField(max_length=64)
    signing_subject = models.CharField(max_length=255)
    synthetic = models.BooleanField(default=True)
    status = models.CharField(max_length=16, choices=Status, default=Status.UNKNOWN)
    expires_at = models.DateTimeField(null=True)
    last_checked_at = models.DateTimeField(null=True)
    recheck_at = models.DateTimeField(null=True)

    class Meta:
        """Allow one professional registration per jurisdiction, separate from roles."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "user", "jurisdiction"),
                name="identity_physician_jurisdiction_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "jurisdiction", "registration_number"),
                name="identity_physician_registration_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(jurisdiction__regex=r"^[A-Z]{2}$")
                & ~models.Q(registration_number="")
                & ~models.Q(signing_subject=""),
                name="identity_physician_identity_nonblank",
            ),
        ]

    def __str__(self) -> str:
        """Return only the stable record identifier."""
        return str(self.pk)


class RoleGrant(TenantScopedModel):
    """Owner-provisioned, append-only clinic subtraction from a versioned bundle."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=UserClinicRole.Role.choices)
    permission = models.CharField(max_length=64)
    bundle_version = models.PositiveSmallIntegerField(default=1)
    effect = models.CharField(max_length=6, default="remove")
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)

    class Meta:
        """A grant is never an addition, including through raw SQL."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(effect="remove", bundle_version=1),
                name="identity_rolegrant_remove_only",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="identity_rolegrant_window",
            ),
        ]


class CareTeamMembership(TenantScopedModel):
    """Time-bounded patient scope, never a substitute for canonical staff roles."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    patient_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=UserClinicRole.Role.choices)
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Keep clinical scopes and half-open validity windows explicit."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    role__in=("physician", "nurse", "allied_professional")
                ),
                name="identity_careteam_clinical_role",
            ),
            models.CheckConstraint(
                condition=models.Q(valid_to__isnull=True)
                | models.Q(valid_to__gt=models.F("valid_from")),
                name="identity_careteam_window",
            ),
        ]


class ProfessionalRegistration(TenantScopedModel):
    """Synthetic council evidence; encrypted number, optional legacy CRM linkage.

    A new council is data, not a provider implementation or authority to sign.
    Issuance still requires the existing per-attempt verification and step-up.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    physician_profile = models.ForeignKey(
        PhysicianProfile, on_delete=models.PROTECT, null=True, blank=True
    )
    role = models.CharField(max_length=20, choices=UserClinicRole.Role.choices)
    council = models.CharField(max_length=16)
    number = EncryptedTextField(purpose="identity.registration.number")
    jurisdiction = models.CharField(max_length=2)
    specialty = EncryptedTextField(purpose="identity.registration.specialty", null=True)
    synthetic = models.BooleanField(default=True)
    status = models.CharField(
        max_length=16,
        choices=PhysicianProfile.Status,
        default=PhysicianProfile.Status.UNKNOWN,
    )
    valid_from = models.DateTimeField()
    valid_to = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Deny live evidence and cross-profession widening at the database."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(synthetic=True)
                & models.Q(status__in=PhysicianProfile.Status.values)
                & models.Q(council__regex=r"^[A-Z][A-Z0-9]{1,15}$")
                & models.Q(jurisdiction__regex=r"^[A-Z]{2}$")
                & models.Q(valid_to__gt=models.F("valid_from")),
                name="identity_registration_synthetic",
            ),
            models.CheckConstraint(
                condition=models.Q(role="physician", council="CRM")
                | models.Q(role="nurse", council="COREN")
                | (
                    models.Q(role="allied_professional")
                    & ~models.Q(council__in=("CRM", "COREN"))
                ),
                name="identity_registration_profession",
            ),
        ]


class PhysicianEvidence(models.Model):
    """Append-only normalized registry response, including failed verification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    profile = models.ForeignKey(PhysicianProfile, on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    attempted_at = models.DateTimeField(auto_now_add=True)
    provider = models.CharField(max_length=64)
    reference = models.CharField(max_length=255, blank=True)
    synthetic = models.BooleanField()
    jurisdiction = models.CharField(max_length=2, blank=True)
    registration_number = models.CharField(max_length=64, blank=True)
    signing_subject = models.CharField(max_length=255, blank=True)
    status = models.CharField(max_length=16, choices=PhysicianProfile.Status)
    checked_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField(null=True)
    recheck_at = models.DateTimeField(null=True)
    reason_code = models.CharField(max_length=64)

    def __str__(self) -> str:
        """Return only the stable evidence identifier."""
        return str(self.pk)

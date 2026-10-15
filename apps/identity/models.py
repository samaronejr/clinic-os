"""Identity domain database models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.contrib.postgres.fields import ArrayField
from django.db import models
from django.utils.translation import gettext_lazy as _
from django.utils.translation import pgettext_lazy

from apps.scheduling.timezones import IanaTimezoneField, validate_iana_timezone
from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
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
                UserClinicRole.Role.CLINIC_MANAGER,
                UserClinicRole.Role.ORG_ADMIN,
            ),
        ).exists()


class Organization(models.Model):
    """Root tenant organization."""

    class Status(models.TextChoices):
        """Pending self-signup grants no runtime clinic authority."""

        ACTIVE = "active", _("Active")
        PENDING_ACTIVATION = "pending_activation", _("Pending activation")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255)
    cnpj = models.CharField(max_length=14)
    status = models.CharField(max_length=20, choices=Status, default=Status.ACTIVE)

    class Meta:
        """Keep pending capture out of the legacy plaintext identity fields."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(status__in=("active", "pending_activation")),
                name="identity_organization_status",
            ),
            models.CheckConstraint(
                condition=~models.Q(status="pending_activation")
                | models.Q(name="", cnpj=""),
                name="identity_pending_capture_protected",
            ),
        ]

    def __str__(self) -> str:
        """Return the organization name."""
        return self.name


class Clinic(models.Model):
    """Clinic owned by one organization."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id: uuid.UUID
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
    ai_policy = models.JSONField(default=dict, db_default={}, blank=True)
    # Scheduling lifecycle policy (todo 22): patient self-booking becomes a
    # non-occupying request only when staff approval is required; holds expire
    # after this many minutes of database time.
    self_booking_requires_approval = models.BooleanField(
        default=False, db_default=False
    )
    hold_ttl_minutes = models.PositiveSmallIntegerField(default=10, db_default=10)
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
            models.CheckConstraint(
                condition=models.Q(hold_ttl_minutes__gte=1)
                & models.Q(hold_ttl_minutes__lte=60),
                name="identity_config_hold_ttl_bounds",
            ),
        ]

    def __str__(self) -> str:
        """Return the stable snapshot identifier, not clinic contact data."""
        return str(self.pk)


class CurrentMembershipManager(models.Manager["UserClinicRole"]):
    """Expose current assignments while retained history stays explicit."""

    def get_queryset(self) -> models.QuerySet[UserClinicRole]:
        """Exclude revoked assignments from authority-bearing ORM readers."""
        return super().get_queryset().filter(revoked_at__isnull=True)


class UserClinicRole(models.Model):
    """Canonical clinic membership role assignment."""

    class Role(models.TextChoices):
        """Stored role values."""

        OWNER = "owner", pgettext_lazy("account owner", "Owner")
        PHYSICIAN = "physician", _("Physician")
        RECEPTIONIST = "receptionist", _("Receptionist")
        CLINIC_ADMIN = "clinic_admin", _("Clinic admin")
        NURSE = "nurse", _("Nurse")
        ALLIED_PROFESSIONAL = "allied_professional", _("Allied professional")
        SCHEDULER = "scheduler", _("Scheduler")
        CLINIC_MANAGER = "clinic_manager", _("Clinic manager")
        FINANCE = "finance", _("Finance")
        ORG_ADMIN = "org_admin", _("Organization admin")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    role = models.CharField(max_length=20, choices=Role.choices)
    workflow_bundle_version = models.PositiveSmallIntegerField(default=1)
    revoked_at = models.DateTimeField(null=True, blank=True)

    objects = CurrentMembershipManager()
    all_objects = models.Manager["UserClinicRole"]()

    class Meta:
        """Prevent duplicate exact role assignments."""

        base_manager_name = "all_objects"
        default_manager_name = "objects"
        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "user", "role"),
                condition=models.Q(revoked_at__isnull=True),
                name="identity_userclinicrole_assignment_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(workflow_bundle_version__in=(1, 2)),
                name="identity_workflow_bundle_version",
            ),
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


class ServicePrincipal(TenantScopedModel):
    """One immutable machine identity per database login, never a staff user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    name = models.SlugField(max_length=64)
    db_identity = models.CharField(max_length=63, unique=True)
    purpose = models.SlugField(max_length=64)
    grant_set_version = models.PositiveSmallIntegerField(default=1)
    active = models.BooleanField(default=True)

    class Meta:
        """Bind one credential to one principal; labels contain no personal data."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"), name="identity_principal_org_id_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(grant_set_version=1)
                & models.Q(db_identity__regex=r"^clinic_agent(_[a-z0-9_]+)?$")
                & models.Q(name__regex=r"^[a-z0-9][a-z0-9_-]{0,63}$")
                & models.Q(purpose__regex=r"^[a-z0-9][a-z0-9_-]{0,63}$"),
                name="identity_principal_v1_identity",
            ),
        ]


class ServicePrincipalGrant(TenantScopedModel):
    """Explicit machine scope; new permissions require a reviewed grant version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    principal = models.ForeignKey(ServicePrincipal, on_delete=models.PROTECT)
    permission = models.CharField(max_length=64)
    subject_scope = models.CharField(max_length=16, default="clinic")
    active = models.BooleanField(default=True)

    class Meta:
        """V1 exposes availability read only, not any clinical or financial action."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("principal", "permission", "subject_scope"),
                condition=models.Q(active=True),
                name="identity_principal_active_grant",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    permission="appointment.read", subject_scope="clinic"
                ),
                name="identity_principal_grant_v1_scope",
            ),
        ]


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
                condition=models.Q(effect="remove", bundle_version__in=(1, 2)),
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


class SavedView(TenantScopedModel):
    """One user's saved workspace view: a destination plus closed parameters.

    Parameters are short slugs from each destination's registered vocabulary
    (``apps.core.saved_views``), never free text, so a saved view can carry
    no patient data. Row security binds every row to ``app.current_user_id``
    and a role the user still holds in the clinic; the runtime role may insert
    and archive, never delete.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    destination = models.CharField(max_length=32)
    params = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Keep destinations slug-shaped and one active copy per view."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(destination__regex=r"^[a-z][a-z-]{0,31}$"),
                name="identity_savedview_destination_slug",
            ),
            models.UniqueConstraint(
                fields=("user", "clinic", "destination", "params"),
                condition=models.Q(archived_at__isnull=True),
                name="identity_savedview_active_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Return only the stable record identifier."""
        return str(self.pk)


class SignupRegistration(TenantScopedModel):
    """Owner-only protected capture awaiting the real subscription activation.

    The first administrator has an inactive login and no membership. Desired
    login, legal and professional identity live only in the encrypted payload
    until the owning activation service accepts the registration.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id: uuid.UUID
    first_admin_id: uuid.UUID
    organization = models.OneToOneField(Organization, on_delete=models.PROTECT)
    first_admin = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    idempotency_key = models.UUIDField(unique=True)
    payload = EncryptedJSONField(purpose="identity.signup.capture")
    payload_fingerprint = models.BinaryField()
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return only the opaque registration identity."""
        return str(self.pk)


class OnboardingProgress(TenantScopedModel):
    """Resumable, non-clinical completion receipts for registered clinic steps."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id: uuid.UUID
    clinic_id: uuid.UUID
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    step_key = models.CharField(max_length=80)
    completed_at = models.DateTimeField(null=True, blank=True)
    reviewed_fingerprint = models.BinaryField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep exactly one persisted receipt per clinic and startup key."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "step_key"),
                name="identity_onboarding_clinic_step_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Return only the opaque receipt identifier."""
        return str(self.pk)


class StaffInvitation(TenantScopedModel):
    """Protected invitation and interrupted enrollment, never a bearer grant."""

    class State(models.TextChoices):
        """Only pending invitations can complete their enrollment."""

        PENDING = "pending", _("Pending")
        ACCEPTED = "accepted", _("Accepted")
        EXPIRED = "expired", _("Expired")
        REVOKED = "revoked", _("Revoked")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id: uuid.UUID
    clinic_id: uuid.UUID
    pending_user_id: uuid.UUID
    invited_by_id: uuid.UUID
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    email_hash = models.BinaryField()
    email = EncryptedTextField(purpose="identity.staff.invitation.email")
    bundle = models.CharField(max_length=32, choices=UserClinicRole.Role)
    token_hash = models.BinaryField(unique=True)
    token_nonce = models.UUIDField(default=uuid.uuid4, editable=False)
    state = models.CharField(max_length=16, choices=State, default=State.PENDING)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)
    accepted_at = models.DateTimeField(null=True, blank=True)
    revoked_at = models.DateTimeField(null=True, blank=True)
    invited_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="issued_staff_invitations",
    )
    pending_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="staff_invitations",
    )
    desired_username = EncryptedTextField(
        purpose="identity.staff.invitation.username", blank=True, null=True
    )
    password_set_at = models.DateTimeField(null=True, blank=True)
    device = models.OneToOneField(
        "otp_totp.TOTPDevice", on_delete=models.PROTECT, null=True, blank=True
    )

    class Meta:
        """Bind complete state vocabulary and coherent terminal receipts."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("pending", "accepted", "expired", "revoked")
                ),
                name="identity_invitation_state",
            ),
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="identity_invitation_expiry",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="accepted",
                        accepted_at__isnull=False,
                        password_set_at__isnull=False,
                        device__isnull=False,
                    )
                    | (~models.Q(state="accepted") & models.Q(accepted_at__isnull=True))
                ),
                name="identity_invitation_acceptance",
            ),
        ]

    def __str__(self) -> str:
        """Return only the invitation id; email/token are never reflected."""
        return str(self.pk)


class RecoveryBatch(models.Model):
    """One hash-only recovery enrollment tied to a confirmed authenticator."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id: uuid.UUID
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    device = models.ForeignKey("otp_totp.TOTPDevice", on_delete=models.PROTECT)
    static_device = models.OneToOneField(
        "otp_static.StaticDevice", on_delete=models.PROTECT
    )
    issued_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        """Return only the recovery batch identifier."""
        return str(self.pk)


class RecoveryCode(models.Model):
    """One atomic one-use digest; plaintext codes have no storage column."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(RecoveryBatch, on_delete=models.PROTECT)
    code_hash = models.BinaryField(unique=True)
    used_at = models.DateTimeField(null=True, blank=True)

    def __str__(self) -> str:
        """Return only the code record id."""
        return str(self.pk)


class AuthenticatorRetirement(models.Model):
    """Retained retirement prevents an old unconfirmed seed from being resumed."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id: uuid.UUID
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    device = models.OneToOneField(
        "otp_totp.TOTPDevice",
        on_delete=models.PROTECT,
        related_name="lifecycle_retirement",
    )
    retired_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        """Return only the retirement record id."""
        return str(self.pk)


class AuthenticatorReset(TenantScopedModel):
    """Audited reset whose full delay starts only after actual user notification."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id: uuid.UUID
    clinic_id: uuid.UUID
    user_id: uuid.UUID
    requested_by_id: uuid.UUID
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="authenticator_resets",
    )
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="requested_authenticator_resets",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    notified_at = models.DateTimeField(null=True, blank=True)
    available_at = models.DateTimeField(null=True, blank=True)
    executed_at = models.DateTimeField(null=True, blank=True)
    notification_operation = models.OneToOneField(
        "comms.IntegrationOperation", on_delete=models.PROTECT, null=True, blank=True
    )

    def __str__(self) -> str:
        """Return only the reset request id."""
        return str(self.pk)


class SessionRecord(models.Model):
    """User-owned, hash-only session inventory with coarse network/client metadata."""

    class UserAgentFamily(models.TextChoices):
        """No version, platform or raw User-Agent is retained."""

        CHROMIUM = "chromium", _("Chromium")
        FIREFOX = "firefox", _("Firefox")
        SAFARI = "safari", _("Safari")
        OTHER = "other", _("Other browser")

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user_id: uuid.UUID
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    session_key_hash = models.BinaryField(unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now_add=True)
    ip_network = models.CharField(max_length=48, blank=True)
    user_agent_family = models.CharField(
        max_length=16,
        choices=UserAgentFamily,
        default=UserAgentFamily.OTHER,
    )
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True, blank=True)
    superseded_at = models.DateTimeField(null=True, editable=False)
    verification_device = models.ForeignKey(
        "otp_totp.TOTPDevice",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    verified_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Distinguish superseded keys without weakening revocation."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=(
                    models.Q(superseded_at__isnull=True)
                    | models.Q(revoked_at__isnull=False)
                ),
                name="identity_session_superseded_is_revoked",
            ),
        ]

    def __str__(self) -> str:
        """Return only the session record id, never its hash or metadata."""
        return str(self.pk)


class PatientDelegateGrant(TenantScopedModel):
    """Verified representation authority, separate from staff membership.

    Contact possession supplies no authority. The two enrollments and evidence
    are bound to the same clinic by the database; scope is immutable, and an
    expired or revoked grant never authorizes a patient request.
    """

    class State(models.TextChoices):
        """Only verification followed by activation can grant access."""

        PENDING = "pending_verification", "Pending verification"
        VERIFIED = "verified", "Verified"
        REJECTED = "rejected", "Rejected"
        ACTIVE = "active", "Active"
        REVOKED = "revoked", "Revoked"

    class AuthorityKind(models.TextChoices):
        """Representation kinds requiring independent staff verification."""

        GUARDIAN = "legal_guardian_minor", "Legal guardian of a minor"
        CURATOR = "curator", "Curator"
        ADULT = "patient_authorized_adult", "Patient-authorized adult"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey(Clinic, on_delete=models.PROTECT)
    represented_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="representation_grants",
    )
    delegate_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="delegate_grants",
    )
    evidence_attachment = models.ForeignKey(
        "ehr.ClinicalAttachment", on_delete=models.PROTECT
    )
    authority_kind = models.CharField(max_length=32, choices=AuthorityKind.choices)
    purposes = ArrayField(models.CharField(max_length=32))
    operations = ArrayField(models.CharField(max_length=32))
    state = models.CharField(
        max_length=24, choices=State.choices, default=State.PENDING
    )
    revision = models.PositiveIntegerField(default=1)
    idempotency_key = models.UUIDField()
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="delegate_requests",
    )
    grantor_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT, null=True
    )
    renewal_of = models.ForeignKey("self", on_delete=models.PROTECT, null=True)
    verified_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="delegate_verifications",
    )
    verified_at = models.DateTimeField(null=True)
    activated_at = models.DateTimeField(null=True)
    revoked_at = models.DateTimeField(null=True)
    expires_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Constrain lifecycle and immutable request identity."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"), name="identity_delegate_org_id_uniq"
            ),
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"),
                name="identity_delegate_request_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "pending_verification",
                        "verified",
                        "rejected",
                        "active",
                        "revoked",
                    )
                )
                & models.Q(
                    authority_kind__in=(
                        "legal_guardian_minor",
                        "curator",
                        "patient_authorized_adult",
                    )
                )
                & models.Q(revision__gte=1)
                & models.Q(expires_at__gt=models.F("created_at")),
                name="identity_delegate_lifecycle",
            ),
            models.CheckConstraint(
                condition=~models.Q(
                    represented_enrollment=models.F("delegate_enrollment")
                ),
                name="identity_delegate_distinct_subject",
            ),
        ]

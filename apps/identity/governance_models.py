"""Purpose-bound, expiring emergency and support access histories."""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class BreakGlassGrant(TenantScopedModel):
    """Read-only emergency scope; it never creates professional or write rights."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    clinician = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    encounter = models.ForeignKey("ehr.Encounter", null=True, on_delete=models.PROTECT)
    reason = EncryptedTextField(purpose="identity.break_glass.reason")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    reviewed_at = models.DateTimeField(null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    review_task = models.OneToOneField(
        "workflows.Task", null=True, on_delete=models.PROTECT
    )

    class Meta:
        """Persist the maximum four-hour interval even for direct SQL writers."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at"))
                & models.Q(expires_at__lte=models.F("created_at") + timedelta(hours=4)),
                name="identity_break_glass_four_hours",
            ),
        ]


class SupportAccessGrant(TenantScopedModel):
    """Customer-approved masked diagnostics; the target is a named human user."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    support_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    purpose = EncryptedTextField(purpose="identity.support.purpose")
    created_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField()
    revoked_at = models.DateTimeField(null=True)
    reviewed_at = models.DateTimeField(null=True)

    class Meta:
        """Support approval cannot be unbounded or already expired."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(expires_at__gt=models.F("created_at")),
                name="identity_support_bounded",
            ),
        ]


class GovernanceNotice(TenantScopedModel):
    """Immediate durable manager inbox and patient access-log entries."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    break_glass = models.ForeignKey(BreakGlassGrant, on_delete=models.PROTECT)
    audience = models.CharField(max_length=8)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """One content-free notice per audience is part of grant issuance."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(audience__in=("manager", "patient")),
                name="identity_governance_audience",
            ),
            models.UniqueConstraint(
                fields=("break_glass", "audience"),
                name="identity_governance_notice_once",
            ),
        ]


class UnitGovernanceChange(TenantScopedModel):
    """Immutable unit-policy, template, credential and recovery references."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    kind = models.CharField(max_length=24)
    references = EncryptedJSONField(purpose="identity.unit_governance.references")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)
    target_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    access_denied = models.BooleanField(null=True)
    policy_version = models.PositiveIntegerField(null=True)

    class Meta:
        """A closed vocabulary never interprets arbitrary actions or tools."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=(
                        "unit_membership",
                        "template_propagation",
                        "credential_rotation",
                        "recovery_exercise",
                    )
                ),
                name="identity_unit_governance_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind="unit_membership",
                    target_user__isnull=False,
                    access_denied__isnull=False,
                    policy_version__gte=1,
                )
                | (
                    ~models.Q(kind="unit_membership")
                    & models.Q(
                        target_user__isnull=True,
                        access_denied__isnull=True,
                        policy_version__isnull=True,
                    )
                ),
                name="identity_unit_policy_shape",
            ),
            models.UniqueConstraint(
                fields=("clinic", "target_user", "policy_version"),
                condition=models.Q(kind="unit_membership"),
                name="identity_unit_policy_version",
            ),
        ]

"""Tenant-scoped database model primitives."""

from typing import ClassVar

from django.db import models

from apps.identity.models import Organization


class TenantScopedModel(models.Model):
    """Abstract base for rows owned by one organization."""

    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)

    class Meta:
        """Keep the tenancy primitive out of the database schema."""

        abstract = True


class TenantProbe(TenantScopedModel):
    """Internal concrete sentinel for tenancy enforcement."""

    label = models.CharField(max_length=255)

    def __str__(self) -> str:
        """Return the internal probe label."""
        return self.label


class TenantDataKey(models.Model):
    """One wrapped tenant data-encryption key version per row.

    Only wrapped key bytes are persisted; the plaintext DEK exists only
    inside the resolver boundary during unwrap. The runtime role holds no
    grants on this table: key material is reachable solely through the
    reviewed envelope functions. ``organization`` is PROTECT so key history
    survives organization teardown for recovery and legal hold.
    """

    class Status(models.TextChoices):
        """Exactly one active version encrypts; retired versions only decrypt."""

        ACTIVE = "active", "Active"
        RETIRED = "retired", "Retired"

    id = models.UUIDField(primary_key=True, editable=False)
    organization = models.ForeignKey(Organization, on_delete=models.PROTECT)
    key_version = models.PositiveIntegerField()
    wrapped_key = models.BinaryField()
    status = models.CharField(max_length=16, choices=Status.choices)
    created_at = models.DateTimeField()
    retired_at = models.DateTimeField(null=True)

    class Meta:
        """Bind versions, status vocabulary and retirement coherence."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "key_version"),
                name="tenancy_dek_version_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=("active", "retired")),
                name="tenancy_dek_status_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="active", retired_at__isnull=True)
                    | models.Q(status="retired", retired_at__isnull=False)
                ),
                name="tenancy_dek_retirement_check",
            ),
        ]

    def __str__(self) -> str:
        """Return only the non-secret version identity."""
        return f"{self.organization_id}:{self.key_version}:{self.status}"

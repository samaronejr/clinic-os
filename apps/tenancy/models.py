"""Tenant-scoped database model primitives."""

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

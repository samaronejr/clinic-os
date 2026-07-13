"""Identity domain database models."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.contrib.auth.models import AbstractUser
from django.db import models

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

    class Meta:
        """Define the composite foreign-key target."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "id"),
                name="identity_clinic_org_id_uniq",
            )
        ]

    def __str__(self) -> str:
        """Return the clinic name."""
        return self.name


class UserClinicRole(models.Model):
    """Canonical clinic membership role assignment."""

    class Role(models.TextChoices):
        """Stored role values."""

        OWNER = "owner", "Owner"
        PHYSICIAN = "physician", "Physician"
        RECEPTIONIST = "receptionist", "Receptionist"
        CLINIC_ADMIN = "clinic_admin", "Clinic admin"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    organization = models.ForeignKey(Organization, on_delete=models.CASCADE)
    clinic = models.ForeignKey(Clinic, on_delete=models.CASCADE)
    role = models.CharField(max_length=20, choices=Role.choices)

    def __str__(self) -> str:
        """Return stable identifiers and the stored role value."""
        return f"{self.user_id}:{self.clinic_id}:{self.role}"

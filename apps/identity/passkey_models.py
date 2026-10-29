"""Organization-scoped, user-owned WebAuthn public credentials."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class Passkey(TenantScopedModel):
    """Retain public material and irreversible removal, never a private key."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    organization_id: uuid.UUID
    user_id: uuid.UUID
    clinic_id: uuid.UUID
    organization = models.ForeignKey("identity.Organization", on_delete=models.PROTECT)
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    credential_id = models.BinaryField(unique=True)
    public_key = models.BinaryField()
    sign_count = models.PositiveBigIntegerField(default=0)
    transports = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    last_used = models.DateTimeField(null=True, blank=True)
    nickname = EncryptedTextField(
        purpose="identity.passkey.nickname", null=True, blank=True
    )
    removed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        """Keep the WebAuthn unsigned counter bounded independently of callers."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(sign_count__lte=4294967295),
                name="identity_passkey_counter_uint32",
            ),
        ]

    def __str__(self) -> str:
        """Expose only the opaque record id, never credential or user data."""
        return str(self.pk)

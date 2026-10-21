"""Retained restricted-note authority and reviewed prior-note copy receipts."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.models import TenantScopedModel


class RestrictedNote(TenantScopedModel):
    """A document restriction is retained across its corrections and versions."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    document = models.OneToOneField("ehr.ClinicalDocument", on_delete=models.PROTECT)
    restricted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)


class NoteAccessGrant(TenantScopedModel):
    """One additional read requirement, never staff membership or a care grant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    note = models.ForeignKey(RestrictedNote, on_delete=models.PROTECT)
    grantee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    granted_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    created_at = models.DateTimeField(auto_now_add=True)
    revoked_at = models.DateTimeField(null=True)

    class Meta:
        """Revocation never overwrites or resurrects an earlier grant."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("note", "grantee"),
                condition=models.Q(revoked_at__isnull=True),
                name="ehr_note_one_active_grant",
            )
        ]


class CopiedNoteBlock(TenantScopedModel):
    """Retain source identity and an explicit review at the current draft revision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    target = models.ForeignKey(
        "ehr.ClinicalDocumentVersion",
        on_delete=models.PROTECT,
        related_name="copied_blocks",
    )
    source = models.ForeignKey(
        "ehr.ClinicalDocumentVersion", on_delete=models.PROTECT, related_name="+"
    )
    section = models.CharField(max_length=16)
    command_id = models.UUIDField()
    inserted_revision = models.PositiveIntegerField()
    reviewed_revision = models.PositiveIntegerField(null=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True, related_name="+"
    )
    reviewed_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """The target and command are a replay-stable copy receipt."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("target", "command_id"), name="ehr_copy_command_unique"
            )
        ]

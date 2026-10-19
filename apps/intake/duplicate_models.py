"""Retained deterministic review hints, without plaintext identity shadows."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.contrib.postgres.fields import ArrayField
from django.contrib.postgres.indexes import GinIndex
from django.db import models

from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint


class PatientDuplicateIndex(TenantScopedModel):
    """Append one HMAC-only snapshot when identity or contact data changes."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    name_birth_index = models.BinaryField(max_length=32, null=True, editable=False)
    contact_indexes = ArrayField(
        models.BinaryField(max_length=32), default=list, editable=False
    )
    index_key_version = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep one immutable snapshot per patient version."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "patient", "version"),
                name="intake_duplicate_index_version_uniq",
            ),
        ]
        indexes: ClassVar[list[models.Index]] = [
            models.Index(
                fields=("organization", "name_birth_index"),
                name="intake_dup_name_birth_idx",
            ),
            GinIndex(fields=("contact_indexes",), name="intake_dup_contacts_gin"),
        ]


class DuplicateCandidate(TenantScopedModel):
    """One ordered enrollment pair awaiting explicit human adjudication."""

    class State(models.TextChoices):
        """The complete review lifecycle; no signal makes a merge."""

        OPEN = "open", "Open"
        DISMISSED = "dismissed", "Dismissed"
        MERGED = "merged", "Merged"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment_a = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="duplicate_candidates_a",
    )
    enrollment_b = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="duplicate_candidates_b",
    )
    signals = ArrayField(models.CharField(max_length=32))
    state = models.CharField(max_length=16, choices=State.choices, default=State.OPEN)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Deduplicate reversed pairs while retaining closed decisions."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "clinic", "enrollment_a", "enrollment_b"),
                name="intake_duplicate_candidate_pair_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(enrollment_a__lt=models.F("enrollment_b")),
                name="intake_duplicate_candidate_order_check",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=("open", "dismissed", "merged")),
                name="intake_duplicate_candidate_state_check",
            ),
        ]

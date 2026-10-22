"""Immutable adjudications and reversible aliases, never rewritten history."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint

    from apps.intake.patient_access import IssuedInvitation


class MergeDecision(TenantScopedModel):
    """Retain the actor, exact step-up proof and reviewed demographics versions."""

    # One-time secrets are an in-memory result, never a persisted model field.
    issued_invitations: tuple[IssuedInvitation, ...] = ()
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    candidate = models.OneToOneField(
        "intake.DuplicateCandidate", on_delete=models.PROTECT
    )
    decision = models.CharField(max_length=16)
    adjudicator = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    verified_session = models.ForeignKey(
        "identity.SessionRecord", on_delete=models.PROTECT
    )
    step_up_verified_at = models.DateTimeField()
    merged_patient = models.ForeignKey(
        "intake.Patient", on_delete=models.PROTECT, related_name="merge_decisions"
    )
    survivor_patient = models.ForeignKey(
        "intake.Patient", on_delete=models.PROTECT, related_name="survivor_decisions"
    )
    merged_version = models.PositiveIntegerField()
    survivor_version = models.PositiveIntegerField()
    result_version = models.PositiveIntegerField(null=True)
    reason = EncryptedTextField(purpose="intake.merge.reason")
    field_choices = EncryptedJSONField(purpose="intake.merge.field_choices")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Bind one decision to one candidate and two distinct identities."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(decision__in=("merge", "dismiss")),
                name="intake_merge_decision_kind_check",
            ),
            models.CheckConstraint(
                condition=~models.Q(merged_patient=models.F("survivor_patient")),
                name="intake_merge_distinct_patients_check",
            ),
            models.UniqueConstraint(
                fields=("organization", "id"), name="intake_merge_org_id_uniq"
            ),
        ]


class PatientAlias(TenantScopedModel):
    """An active lineage edge; reversal deactivates it exactly once."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    merged_patient = models.ForeignKey(
        "intake.Patient", on_delete=models.PROTECT, related_name="outgoing_aliases"
    )
    survivor_patient = models.ForeignKey(
        "intake.Patient", on_delete=models.PROTECT, related_name="incoming_aliases"
    )
    merge_decision = models.OneToOneField(MergeDecision, on_delete=models.PROTECT)
    active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    deactivated_at = models.DateTimeField(null=True)
    deactivated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, null=True
    )
    deactivation_session = models.ForeignKey(
        "identity.SessionRecord", on_delete=models.PROTECT, null=True
    )
    deactivation_reason = EncryptedTextField(
        purpose="intake.alias.deactivation_reason", null=True
    )

    class Meta:
        """Permit one outgoing active edge while preserving every earlier edge."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "merged_patient"),
                condition=models.Q(active=True),
                name="intake_alias_active_patient_uniq",
            ),
            models.CheckConstraint(
                condition=~models.Q(merged_patient=models.F("survivor_patient")),
                name="intake_alias_distinct_patients_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        active=True,
                        deactivated_at__isnull=True,
                        deactivated_by__isnull=True,
                        deactivation_session__isnull=True,
                        deactivation_reason__isnull=True,
                    )
                    | models.Q(
                        active=False,
                        deactivated_at__isnull=False,
                        deactivated_by__isnull=False,
                        deactivation_session__isnull=False,
                        deactivation_reason__isnull=False,
                    )
                ),
                name="intake_alias_deactivation_check",
            ),
        ]


class PatientMergeTransfer(TenantScopedModel):
    """Retain the explicit per-app link from a forward row to its successor."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    merge_decision = models.ForeignKey(MergeDecision, on_delete=models.PROTECT)
    kind = models.CharField(max_length=32)
    original_id = models.CharField(max_length=64)
    replacement_id = models.CharField(max_length=64)
    action = models.CharField(max_length=32)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Record exactly one transfer outcome per original row and decision."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("merge_decision", "kind", "original_id"),
                name="intake_merge_transfer_original_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind__in=(
                        "appointment",
                        "waitlist",
                        "contact",
                        "preference",
                        "access_grant",
                    )
                ),
                name="intake_merge_transfer_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    action__in=("recreated", "kept_survivor", "revoked_and_reissued")
                ),
                name="intake_merge_transfer_action_check",
            ),
        ]

"""Retention policies, legal holds, releases and export receipts.

Every row is append- or transition-bound by database triggers: identities,
bindings and content are immutable, deletions are forbidden, and the only
permitted mutations are the explicit lifecycle transitions the services
perform. Nothing here deletes clinical data; disposal is a decision record,
never an automatic purge.
"""

from __future__ import annotations

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.db import models

from apps.tenancy.models import TenantScopedModel

RECORD_CLASSES: Final = (
    "ehr.encounter",
    "ehr.document_version",
    "ehr.clinical_attachment",
    "ehr.history_assessment",
    "ehr.problem",
    "ehr.allergy",
)


class RetentionPolicy(TenantScopedModel):
    """One versioned retention rule per record class; approval is explicit."""

    class State(models.TextChoices):
        """Only an approved policy can ever make a record disposal-eligible."""

        PROPOSED = "proposed", "Proposta"
        APPROVED = "approved", "Aprovada"
        RETIRED = "retired", "Retirada"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    record_class = models.CharField(max_length=64)
    version = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State, default=State.PROPOSED)
    retention_days = models.PositiveIntegerField(null=True, blank=True)
    proposed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="retention_policies_proposed",
    )
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="retention_policies_approved",
    )
    approved_at = models.DateTimeField(null=True, blank=True)
    retired_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="retention_policies_retired",
    )
    retired_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep one ordered version chain and at most one approved policy."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "record_class", "version"),
                name="retention_policy_version_uniq",
            ),
            models.UniqueConstraint(
                fields=("clinic", "record_class"),
                condition=models.Q(state="approved"),
                name="retention_policy_one_approved",
            ),
            models.CheckConstraint(
                condition=models.Q(record_class__in=RECORD_CLASSES),
                name="retention_policy_class_check",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=["proposed", "approved", "retired"]),
                name="retention_policy_state_check",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1),
                name="retention_policy_version_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        state="proposed",
                        approved_by__isnull=True,
                        approved_at__isnull=True,
                        retired_by__isnull=True,
                        retired_at__isnull=True,
                    ),
                    models.Q(
                        state="approved",
                        approved_by__isnull=False,
                        approved_at__isnull=False,
                        retired_by__isnull=True,
                        retired_at__isnull=True,
                    ),
                    models.Q(
                        state="retired",
                        retired_by__isnull=False,
                        retired_at__isnull=False,
                    ),
                    _connector="OR",
                ),
                name="retention_policy_transition_check",
            ),
        ]


class LegalHold(TenantScopedModel):
    """One authority-bound hold on one clinical record; release is recorded."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    record_class = models.CharField(max_length=64)
    record_id = models.UUIDField()
    authority = models.CharField(max_length=255)
    reason = models.CharField(max_length=255)
    placed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="legal_holds_placed",
    )
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="legal_holds_released",
    )
    released_at = models.DateTimeField(null=True, blank=True)
    release_authority = models.CharField(max_length=255, blank=True, default="")
    release_reason = models.CharField(max_length=255, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Keep hold vocabulary and lifecycle coherent in the database."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(record_class__in=RECORD_CLASSES),
                name="retention_hold_class_check",
            ),
            models.CheckConstraint(
                condition=models.Q(authority__regex=r"\S")
                & models.Q(reason__regex=r"\S"),
                name="retention_hold_authority_reason_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        released_at__isnull=True,
                        released_by__isnull=True,
                        release_authority="",
                        release_reason="",
                    ),
                    models.Q(
                        released_at__isnull=False,
                        released_by__isnull=False,
                        release_authority__regex=r"\S",
                        release_reason__regex=r"\S",
                    ),
                    _connector="OR",
                ),
                name="retention_hold_release_check",
            ),
        ]


class RecordRelease(TenantScopedModel):
    """One explicit release of an exact document version to its patient."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    version = models.ForeignKey("ehr.ClinicalDocumentVersion", on_delete=models.PROTECT)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="record_releases_made",
    )
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="record_releases_revoked",
    )
    revoked_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """At most one active release per exact version."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("version",),
                condition=models.Q(revoked_at__isnull=True),
                name="retention_release_one_active",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    models.Q(revoked_at__isnull=True, revoked_by__isnull=True),
                    models.Q(revoked_at__isnull=False, revoked_by__isnull=False),
                    _connector="OR",
                ),
                name="retention_release_revocation_check",
            ),
        ]


class RecordExport(TenantScopedModel):
    """The durable receipt for one released-record export package."""

    class Kind(models.TextChoices):
        """Who the export was produced for."""

        STAFF = "staff", "Equipe"
        PATIENT = "patient", "Paciente"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16, choices=Kind)
    requested_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
    )
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT, null=True, blank=True
    )
    record_count = models.PositiveIntegerField()
    manifest = models.JSONField(default=dict)
    manifest_digest = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Bind each export to exactly one requesting principal."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(kind__in=["staff", "patient"]),
                name="retention_export_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        kind="staff",
                        requested_by__isnull=False,
                        patient_session__isnull=True,
                    ),
                    models.Q(
                        kind="patient",
                        requested_by__isnull=True,
                        patient_session__isnull=False,
                    ),
                    _connector="OR",
                ),
                name="retention_export_actor_check",
            ),
            models.CheckConstraint(
                condition=models.Q(manifest_digest__regex=r"^[0-9a-f]{64}$"),
                name="retention_export_digest_check",
            ),
        ]

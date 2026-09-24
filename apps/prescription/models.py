"""Scoped synthetic drafts and retained clinician-entered item snapshots."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import (
    EncryptedBytesField,
    EncryptedJSONField,
    EncryptedTextField,
)
from apps.tenancy.models import TenantScopedModel


class PrescriptionDraft(TenantScopedModel):
    """One resumable synthetic prescription draft per encounter; never an issuance."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    encounter = models.OneToOneField("ehr.Encounter", on_delete=models.PROTECT)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    category = models.CharField(max_length=40)
    contract_version = models.CharField(max_length=64)
    version = models.PositiveIntegerField(default=1)
    state = models.CharField(max_length=16, default="draft")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Keep the synthetic-only contract explicit in persisted data."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(version__gte=1), name="prescription_version_positive"
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=["draft", "discarded"]),
                name="prescription_draft_state",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    category="synthetic_non_controlled",
                    contract_version="synthetic-draft-v1",
                ),
                name="prescription_synthetic_contract",
            ),
        ]


class PrescriptionItem(TenantScopedModel):
    """Immutable item snapshot; values are text, not calculated treatment advice."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    draft = models.ForeignKey(PrescriptionDraft, on_delete=models.PROTECT)
    version = models.PositiveIntegerField()
    position = models.PositiveSmallIntegerField()
    medication_description = EncryptedTextField(
        purpose="prescription.prescriptionitem.medication_description"
    )
    strength_form = EncryptedTextField(
        purpose="prescription.prescriptionitem.strength_form"
    )
    dose = EncryptedTextField(purpose="prescription.prescriptionitem.dose")
    route = EncryptedTextField(purpose="prescription.prescriptionitem.route")
    frequency = EncryptedTextField(purpose="prescription.prescriptionitem.frequency")
    duration = EncryptedTextField(purpose="prescription.prescriptionitem.duration")
    quantity = EncryptedTextField(purpose="prescription.prescriptionitem.quantity")
    instructions = EncryptedTextField(
        purpose="prescription.prescriptionitem.instructions", null=True
    )

    class Meta:
        """Retain every saved version without overwriting medication content."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("draft", "version", "position"),
                name="prescription_item_version_position",
            ),
            models.CheckConstraint(
                condition=models.Q(version__gte=1, position__gte=1, position__lte=20),
                name="prescription_item_bounds",
            ),
        ]
        ordering: ClassVar = ["position"]


class PrescriptionDocument(TenantScopedModel):
    """One immutable rendered artifact bound to an exact draft version.

    Rows are insert-only: the database trigger rejects every update and
    delete, so ``pdf_bytes``, digests and the frozen input snapshot can never
    be rewritten. ``qr_handle`` is a random public verification handle; it is
    not a patient identifier and confers no download authority.
    """

    class State(models.TextChoices):
        """Rendered artifacts await the task-34 signing lifecycle."""

        RENDERED = "rendered", "Renderizado"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    draft = models.ForeignKey(PrescriptionDraft, on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    document_version = models.PositiveIntegerField()
    state = models.CharField(max_length=16, choices=State, default=State.RENDERED)
    render_params = models.JSONField()
    frozen_input = EncryptedJSONField(
        purpose="prescription.prescriptiondocument.frozen_input"
    )
    input_digest = models.CharField(max_length=64)
    pdf_digest = models.CharField(max_length=64)
    pdf_bytes = EncryptedBytesField(
        purpose="prescription.prescriptiondocument.pdf_bytes"
    )
    qr_handle = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Bind one artifact per draft version and one handle per artifact."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("draft", "document_version"),
                name="prescription_document_version_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(document_version__gte=1),
                name="prescription_document_version_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=["rendered"]),
                name="prescription_document_state",
            ),
            models.CheckConstraint(
                condition=models.Q(input_digest__regex=r"^[0-9a-f]{64}$")
                & models.Q(pdf_digest__regex=r"^[0-9a-f]{64}$"),
                name="prescription_document_digests",
            ),
            models.CheckConstraint(
                condition=models.Q(qr_handle__regex=r"^[A-Za-z0-9_-]{32,64}$"),
                name="prescription_document_handle_shape",
            ),
        ]


LIVE_SIGNATURE_STATES: tuple[str, ...] = ("draft", "prepared", "signing")


class SignatureOperation(TenantScopedModel):
    """One explicit signing lifecycle bound to an immutable rendered document.

    States are ``draft`` -> ``prepared`` -> ``signing`` -> ``issued`` |
    ``rehearsal_complete`` | ``failed``; a ``prepared`` operation may also
    fail before dispatch and a live operation may be abandoned to ``failed``.
    ``operation_id`` is the provider-side reference returned when the
    provider accepts the operation. ``issued`` is reserved for an approved
    real provider; a verified synthetic callback reaches only
    ``rehearsal_complete``, never ``issued``. ``authorized_until`` freezes
    the authorization deadline (earliest of step-up expiry and evidence
    recheck/expiry) computed at preparation; issuance past it fails closed.
    Once ``issued`` or ``rehearsal_complete``, the signed output is
    immutable: the database trigger rejects every further update, and the
    original rendered bytes stay untouched on the bound
    ``PrescriptionDocument``.
    """

    class State(models.TextChoices):
        """Explicit lifecycle states; issued/rehearsal_complete/failed end it."""

        DRAFT = "draft", "Rascunho"
        PREPARED = "prepared", "Preparado"
        SIGNING = "signing", "Assinando"
        ISSUED = "issued", "Emitido"
        REHEARSAL_COMPLETE = "rehearsal_complete", "Ensaio concluído"
        FAILED = "failed", "Falhou"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        PrescriptionDocument,
        on_delete=models.PROTECT,
        related_name="signature_operations",
    )
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    provider = models.CharField(max_length=64)
    operation_id = models.CharField(max_length=128, default="")
    state = models.CharField(max_length=24, choices=State, default=State.DRAFT)
    content_digest = models.CharField(max_length=64)
    signer_subject = models.CharField(max_length=255)
    evidence = models.ForeignKey(
        "identity.PhysicianEvidence",
        on_delete=models.PROTECT,
        null=True,
    )
    evidence_snapshot = models.JSONField(null=True)
    authorized_until = models.DateTimeField(null=True)
    signed_bytes = EncryptedBytesField(
        purpose="prescription.signatureoperation.signed_bytes", null=True
    )
    signed_digest = models.CharField(  # noqa: DJ001 - NULL marks unsigned
        max_length=64, null=True
    )
    failure_reason = models.CharField(max_length=64, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    completed_at = models.DateTimeField(null=True)

    class Meta:
        """Bind one live operation per document and one provider reference."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(state__in=LIVE_SIGNATURE_STATES),
                name="prescription_signature_live_unique",
            ),
            models.UniqueConstraint(
                fields=("provider", "operation_id"),
                condition=~models.Q(operation_id=""),
                name="prescription_signature_operation_id_unique",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=[
                        "draft",
                        "prepared",
                        "signing",
                        "issued",
                        "rehearsal_complete",
                        "failed",
                    ]
                ),
                name="prescription_signature_state",
            ),
            models.CheckConstraint(
                condition=models.Q(content_digest__regex=r"^[0-9a-f]{64}$"),
                name="prescription_signature_content_digest",
            ),
            models.CheckConstraint(
                condition=models.Q(signed_digest__regex=r"^[0-9a-f]{64}$")
                | models.Q(signed_digest__isnull=True),
                name="prescription_signature_signed_digest",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=["issued", "rehearsal_complete"],
                        signed_bytes__isnull=False,
                        signed_digest__isnull=False,
                        completed_at__isnull=False,
                    )
                    | ~models.Q(state__in=["issued", "rehearsal_complete"])
                ),
                name="prescription_signature_issued_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state="failed", completed_at__isnull=False)
                    | ~models.Q(state="failed")
                ),
                name="prescription_signature_failed_fields",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(state__in=["signing", "issued", "rehearsal_complete"])
                    & ~models.Q(operation_id="")
                )
                | models.Q(state__in=["draft", "prepared", "failed"]),
                name="prescription_signature_operation_id_state",
            ),
        ]


COMPLETED_SIGNATURE_STATES: tuple[str, ...] = ("issued", "rehearsal_complete")


class PrescriptionDocumentRevocation(TenantScopedModel):
    """One terminal revocation record for a completed document.

    Rows are insert-only: revocation never rewrites the signed bytes or
    the rendered artifact, it only records that the document must no
    longer verify as current. ``reason`` is a fixed vocabulary, never
    free text, so no clinical detail can be published through it.
    """

    class Reason(models.TextChoices):
        """Fixed revocation vocabulary; no clinical content."""

        CLINICAL_ERROR = "clinical_error", "Erro clínico"
        ISSUANCE_ERROR = "issuance_error", "Erro de emissão"
        ISSUER_REQUEST = "issuer_request", "Solicitação do emissor"
        PATIENT_REQUEST = "patient_request", "Solicitação do paciente"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.OneToOneField(PrescriptionDocument, on_delete=models.PROTECT)
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    reason = models.CharField(max_length=32, choices=Reason)
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prescription_revocations_made",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Pin the fixed reason vocabulary."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(
                    reason__in=[
                        "clinical_error",
                        "issuance_error",
                        "issuer_request",
                        "patient_request",
                    ]
                ),
                name="prescription_revocation_reason",
            ),
        ]


class PrescriptionDocumentRelease(TenantScopedModel):
    """One revocable release of a completed document to its patient.

    The release is the only authority for the patient download surface;
    the QR handle never is. Revoking a release restores the row but keeps
    it: ``revoked_at`` marks the end of access, never a deletion.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    document = models.ForeignKey(
        PrescriptionDocument, on_delete=models.PROTECT, related_name="releases"
    )
    encounter = models.ForeignKey("ehr.Encounter", on_delete=models.PROTECT)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    issuer = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    released_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name="prescription_releases_made",
    )
    revoked_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="prescription_releases_revoked",
    )
    revoked_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """At most one active release per document."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("document",),
                condition=models.Q(revoked_at__isnull=True),
                name="prescription_release_active_unique",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(revoked_at__isnull=True, revoked_by__isnull=True)
                    | models.Q(revoked_at__isnull=False, revoked_by__isnull=False)
                ),
                name="prescription_release_revocation_pair",
            ),
        ]


class VerificationProbe(models.Model):
    """One anonymous verification allowance bucket per probe key.

    The table is tenant-agnostic and unreachable by ``clinic_app``: only
    the ``clinic_resolver``-owned allowance function touches it, so the
    runtime role can neither read nor reset the counters.
    """

    probe_key = models.BinaryField(primary_key=True, max_length=32)
    window_start = models.DateTimeField()
    lookups = models.PositiveIntegerField()

    class Meta:
        """Bound the stored counter."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=models.Q(lookups__gte=0),
                name="prescription_probe_lookups_positive",
            ),
        ]

    def __str__(self) -> str:
        """Return a constant label; the probe key is never rendered."""
        return "verification-probe"


class SignatureCallback(TenantScopedModel):
    """One authenticated provider callback bound to a stored operation.

    Rows are insert-only: authentication happens before any tenant or
    operation is resolved, and the verified payload is retained verbatim
    for audit. ``verified_at`` is the moment the boundary validated the
    provider signature, never a provider-supplied timestamp.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    operation = models.ForeignKey(
        SignatureOperation,
        on_delete=models.PROTECT,
        related_name="callbacks",
    )
    event_id = models.CharField(max_length=128)
    payload = models.JSONField()
    verified_at = models.DateTimeField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Deduplicate provider events per operation."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("operation", "event_id"),
                name="prescription_signature_callback_event_unique",
            ),
            models.CheckConstraint(
                condition=~models.Q(event_id=""),
                name="prescription_signature_callback_event_nonblank",
            ),
        ]

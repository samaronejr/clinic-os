"""LGPD data-subject requests: retained, auditable and never a clinical edit."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar, Final

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel

if TYPE_CHECKING:
    from django.db.models.constraints import BaseConstraint

DATA_REQUEST_KIND_VALUES: Final = (
    "access",
    "correction",
    "portability",
    "sharing_information",
    "objection",
)
DATA_REQUEST_STATE_VALUES: Final = ("received", "in_progress", "fulfilled", "rejected")
DATA_REQUEST_REJECTION_VALUES: Final = (
    "duplicate",
    "identity_unverified",
    "legal_retention",
    "outside_scope",
)


class DataSubjectRequest(TenantScopedModel):
    """One request a patient or verified delegate made under LGPD art. 18.

    The row binds the requesting session, the subject enrollment and the
    delegate grant (when a representative acted). Writes happen only through
    ``clinic_app`` resolver functions; a transition trigger enforces the
    lifecycle received -> in_progress -> fulfilled | rejected (and
    received -> rejected), with received -> fulfilled reserved for an
    objection whose withdrawal already happened. Correction details are a
    tenant envelope; the request never edits a clinical record.
    """

    class Kind(models.TextChoices):
        """The closed set of rights this workflow serves."""

        ACCESS = "access", "Access"
        CORRECTION = "correction", "Correction"
        PORTABILITY = "portability", "Portability"
        SHARING_INFORMATION = "sharing_information", "Sharing information"
        OBJECTION = "objection", "Objection"

    class State(models.TextChoices):
        """The request lifecycle; rejection always carries a reason code."""

        RECEIVED = "received", "Received"
        IN_PROGRESS = "in_progress", "In progress"
        FULFILLED = "fulfilled", "Fulfilled"
        REJECTED = "rejected", "Rejected"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    patient = models.ForeignKey("intake.Patient", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="data_subject_requests",
    )
    requester_enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment",
        on_delete=models.PROTECT,
        related_name="+",
    )
    patient_session = models.ForeignKey(
        "intake.PatientSession", on_delete=models.PROTECT, related_name="+"
    )
    delegate_grant = models.ForeignKey(
        "identity.PatientDelegateGrant",
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    kind = models.CharField(max_length=24, choices=Kind.choices)
    purpose = models.CharField(max_length=32, blank=True, default="")
    details = EncryptedTextField(
        purpose="intake.datasubjectrequest.details", null=True, blank=True
    )
    state = models.CharField(
        max_length=16, choices=State.choices, default=State.RECEIVED
    )
    rejection_reason = models.CharField(max_length=24, blank=True, default="")
    received_at = models.DateTimeField()
    due_at = models.DateTimeField()
    closed_at = models.DateTimeField(null=True, blank=True)
    task = models.ForeignKey(
        "workflows.Task", on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        null=True,
        related_name="+",
    )
    updated_at = models.DateTimeField()

    class Meta:
        """Bind one command key per clinic and a coherent lifecycle."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"),
                name="intake_data_request_key_uniq",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="intake_data_request_scope_uniq",
            ),
            models.UniqueConstraint(
                fields=("task",), name="intake_data_request_task_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=DATA_REQUEST_KIND_VALUES),
                name="intake_data_request_kind_check",
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=DATA_REQUEST_STATE_VALUES),
                name="intake_data_request_state_check",
            ),
            models.CheckConstraint(
                condition=(
                    (models.Q(kind="objection") & ~models.Q(purpose=""))
                    | (~models.Q(kind="objection") & models.Q(purpose=""))
                ),
                name="intake_data_request_purpose_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(kind="correction", details__isnull=False)
                    | (~models.Q(kind="correction") & models.Q(details__isnull=True))
                ),
                name="intake_data_request_details_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state="rejected",
                        rejection_reason__in=DATA_REQUEST_REJECTION_VALUES,
                    )
                    | (~models.Q(state="rejected") & models.Q(rejection_reason=""))
                ),
                name="intake_data_request_rejection_check",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=("received", "in_progress"), closed_at__isnull=True
                    )
                    | models.Q(
                        state__in=("fulfilled", "rejected"), closed_at__isnull=False
                    )
                ),
                name="intake_data_request_closed_check",
            ),
            models.CheckConstraint(
                condition=models.Q(due_at__gt=models.F("received_at")),
                name="intake_data_request_due_check",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="intake_data_request_fingerprint_check",
            ),
            models.CheckConstraint(
                condition=models.Q(revision__gte=1),
                name="intake_data_request_revision_check",
            ),
        ]

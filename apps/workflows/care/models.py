"""Care plans and their typed follow-up records (todo 52, D-16).

These rows hold typed references, closed vocabularies and encrypted clinical
text only. Tasks stay the single work entity: an administrative action owns one
``Task`` and mirrors its owner and state, it never becomes a second task store.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedJSONField, EncryptedTextField
from apps.tenancy.models import TenantScopedModel
from apps.workflows.care import vocab

_DIGEST = r"^[0-9a-f]{64}$"


def _in(field: str, values: tuple[str, ...]) -> models.Q:
    return models.Q(**{f"{field}__in": values})


class CarePlan(TenantScopedModel):
    """A clinician-approved plan bound to one finalized note version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    encounter = models.ForeignKey(
        "ehr.Encounter", on_delete=models.PROTECT, related_name="+"
    )
    source_version = models.ForeignKey(
        "ehr.ClinicalDocumentVersion", on_delete=models.PROTECT, related_name="+"
    )
    clinician = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    state = models.CharField(max_length=16, default="active")
    revision = models.PositiveIntegerField(default=1)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """One plan per source version; replay cannot create another."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("source_version",), name="workflows_careplan_source_uniq"
            ),
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_careplan_key"
            ),
            # Target of the composite keys that bind actions, referrals and
            # PROs to their plan's exact clinic.
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_careplan_scope",
            ),
            models.CheckConstraint(
                condition=_in("state", vocab.PLAN_STATES),
                name="workflows_careplan_state",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=_DIGEST),
                name="workflows_careplan_digest",
            ),
        ]


class CareAction(TenantScopedModel):
    """One typed, owned action with a due window and an originating version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    plan = models.ForeignKey(CarePlan, on_delete=models.PROTECT, related_name="actions")
    kind = models.CharField(max_length=24)
    window_start = models.DateTimeField()
    window_end = models.DateTimeField()
    state = models.CharField(max_length=16, default="open")
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    owner_role = models.CharField(max_length=20, blank=True, default="")
    origin_version = models.ForeignKey(
        "ehr.ClinicalDocumentVersion", on_delete=models.PROTECT, related_name="+"
    )
    review_state = models.CharField(max_length=16, default="none")
    approved_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    instruction = EncryptedTextField(
        purpose="workflows.careaction.instruction", null=True
    )
    task = models.OneToOneField(
        "workflows.Task", null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Closed vocabularies; a patient instruction never has a staff task."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_careaction_key"
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_careaction_scope",
            ),
            models.CheckConstraint(
                condition=_in("kind", vocab.ACTION_KINDS),
                name="workflows_careaction_kind",
            ),
            models.CheckConstraint(
                condition=_in("state", vocab.ACTION_STATES),
                name="workflows_careaction_state",
            ),
            models.CheckConstraint(
                condition=_in("review_state", vocab.REVIEW_STATES),
                name="workflows_careaction_review",
            ),
            models.CheckConstraint(
                condition=models.Q(window_end__gt=models.F("window_start")),
                name="workflows_careaction_window",
            ),
            models.CheckConstraint(
                condition=models.Q(owner_user__isnull=True) | models.Q(owner_role=""),
                name="workflows_careaction_one_owner",
            ),
            models.CheckConstraint(
                condition=models.Q(kind="patient_instruction", task__isnull=True)
                | (
                    ~models.Q(kind="patient_instruction") & models.Q(task__isnull=False)
                ),
                name="workflows_careaction_task_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    kind="patient_instruction", instruction__isnull=False
                )
                | (
                    ~models.Q(kind="patient_instruction")
                    & models.Q(instruction__isnull=True)
                ),
                name="workflows_careaction_text_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=_DIGEST),
                name="workflows_careaction_digest",
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=("clinic", "state", "window_end"),
                name="workflows_careaction_due",
            )
        ]


class Referral(TenantScopedModel):
    """Coordination of a referral; the document link is nullable by design."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    plan = models.ForeignKey(
        CarePlan, null=True, on_delete=models.PROTECT, related_name="referrals"
    )
    action = models.OneToOneField(
        CareAction, null=True, on_delete=models.PROTECT, related_name="referral"
    )
    target_kind = models.CharField(max_length=16)
    target_label = EncryptedTextField(
        purpose="workflows.referral.target_label", null=True
    )
    status = models.CharField(max_length=16, default="requested")
    document = models.OneToOneField(
        "prescription.PrescriptionDraft",
        null=True,
        on_delete=models.PROTECT,
        related_name="+",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """A backfilled link has no plan; a planned referral always has both."""

        constraints: ClassVar = [
            models.CheckConstraint(
                condition=_in("target_kind", vocab.REFERRAL_TARGETS),
                name="workflows_referral_target",
            ),
            models.CheckConstraint(
                condition=_in("status", vocab.REFERRAL_STATUSES),
                name="workflows_referral_status",
            ),
            models.CheckConstraint(
                condition=models.Q(plan__isnull=False, action__isnull=False)
                | models.Q(plan__isnull=True, action__isnull=True),
                name="workflows_referral_shape",
            ),
        ]


class Recall(TenantScopedModel):
    """A scheduled return prompt that becomes one human-owned task when due."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    reason = models.CharField(max_length=24)
    interval_days = models.PositiveSmallIntegerField()
    anchor_at = models.DateTimeField()
    due_at = models.DateTimeField()
    owner_role = models.CharField(max_length=20)
    state = models.CharField(max_length=16, default="scheduled")
    task = models.OneToOneField(
        "workflows.Task", null=True, on_delete=models.PROTECT, related_name="+"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Bound intervals; replay cannot schedule a second recall."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_recall_key"
            ),
            models.CheckConstraint(
                condition=_in("reason", vocab.RECALL_REASONS),
                name="workflows_recall_reason",
            ),
            models.CheckConstraint(
                condition=_in("state", vocab.RECALL_STATES),
                name="workflows_recall_state",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    interval_days__gte=1, interval_days__lte=vocab.MAX_RECALL_DAYS
                ),
                name="workflows_recall_interval",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=_DIGEST),
                name="workflows_recall_digest",
            ),
        ]
        indexes: ClassVar = [
            models.Index(fields=("state", "due_at"), name="workflows_recall_due")
        ]


class PROAssignment(TenantScopedModel):
    """A scheduled patient-reported outcome instrument with a human response queue."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT, related_name="+"
    )
    plan = models.ForeignKey(
        CarePlan, null=True, on_delete=models.PROTECT, related_name="pro_assignments"
    )
    instrument_key = models.CharField(max_length=16)
    instrument_version = models.PositiveSmallIntegerField()
    first_due_at = models.DateTimeField()
    interval_days = models.PositiveSmallIntegerField()
    occurrences = models.PositiveSmallIntegerField()
    response_role = models.CharField(max_length=20)
    staffing_policy_ref = models.CharField(max_length=64)
    state = models.CharField(max_length=16, default="active")
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Only available instruments and bounded schedules can be stored."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_pro_key"
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_pro_scope"
            ),
            models.CheckConstraint(
                condition=_in("instrument_key", vocab.PRO_INSTRUMENTS)
                & models.Q(instrument_version=1),
                name="workflows_pro_instrument",
            ),
            models.CheckConstraint(
                condition=_in("state", vocab.PRO_ASSIGNMENT_STATES),
                name="workflows_pro_state",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    interval_days__gte=1, interval_days__lte=vocab.MAX_PRO_INTERVAL_DAYS
                )
                & models.Q(
                    occurrences__gte=1, occurrences__lte=vocab.MAX_PRO_OCCURRENCES
                ),
                name="workflows_pro_schedule",
            ),
            models.CheckConstraint(
                condition=models.Q(staffing_policy_ref__regex=vocab.POLICY_REF_PATTERN),
                name="workflows_pro_policy_ref",
            ),
            models.CheckConstraint(
                condition=_in("response_role", vocab.PRO_RESPONSE_ROLES),
                name="workflows_pro_response_role",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=_DIGEST),
                name="workflows_pro_digest",
            ),
        ]


class CareRefusal(TenantScopedModel):
    """A due care record its stored actor could not process, kept as evidence.

    Written only by the scanner's resolver, never by a staff command: the row
    records who was refused, how often and when the record is listed again,
    so a refusal is neither swallowed nor re-listed every minute.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    record_id = models.UUIDField()
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    attempts = models.PositiveIntegerField(default=1)
    refused_at = models.DateTimeField()
    retry_at = models.DateTimeField()

    class Meta:
        """One evidence row per refused record; retries update it in place."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("kind", "record_id"), name="workflows_carerefusal_record"
            ),
            models.CheckConstraint(
                condition=_in("kind", vocab.SCAN_KINDS),
                name="workflows_carerefusal_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(retry_at__gt=models.F("refused_at"))
                & models.Q(attempts__gte=1),
                name="workflows_carerefusal_retry",
            ),
        ]


class PROOccurrence(TenantScopedModel):
    """One scheduled response slot; absence is recorded as 'no_response' only.

    ``concerning`` only sets the priority of the review task the database emits
    for the response; like the answers, it is visible to the patient's
    clinicians only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    assignment = models.ForeignKey(
        PROAssignment, on_delete=models.PROTECT, related_name="occurrence_set"
    )
    sequence = models.PositiveSmallIntegerField()
    due_at = models.DateTimeField()
    state = models.CharField(max_length=16, default="pending")
    answers = EncryptedJSONField(purpose="workflows.prooccurrence.answers", null=True)
    concerning = models.BooleanField(default=False)
    response_task = models.OneToOneField(
        "workflows.Task", null=True, on_delete=models.PROTECT, related_name="+"
    )
    recorded_at = models.DateTimeField(null=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    revision = models.PositiveIntegerField(default=1)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """A response, a miss and a queue task are mutually consistent."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("assignment", "sequence"), name="workflows_prooccurrence_seq"
            ),
            models.CheckConstraint(
                condition=_in("state", vocab.PRO_OCCURRENCE_STATES),
                name="workflows_prooccurrence_state",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state="responded",
                    answers__isnull=False,
                    response_task__isnull=False,
                )
                | models.Q(
                    state__in=("pending", "no_response"),
                    answers__isnull=True,
                    response_task__isnull=True,
                    concerning=False,
                ),
                name="workflows_prooccurrence_shape",
            ),
        ]
        indexes: ClassVar = [
            models.Index(fields=("state", "due_at"), name="workflows_prooccurrence_due")
        ]

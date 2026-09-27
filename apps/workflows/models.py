"""Durable tasks and version-pinned workflow history (ADR-008)."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.fields import EncryptedTextField
from apps.tenancy.models import TenantScopedModel


class Task(TenantScopedModel):
    """A clinic work item; typed references are not a second clinical record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    kind = models.CharField(max_length=16)
    subject_ref = models.JSONField()
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name="+"
    )
    owner_user = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, on_delete=models.PROTECT, related_name="+"
    )
    owner_role = models.CharField(max_length=20, blank=True, default="")
    due_at = models.DateTimeField()
    priority = models.CharField(max_length=8, default="normal")
    state = models.CharField(max_length=16, default="open")
    escalation_policy_version = models.PositiveSmallIntegerField(default=1)
    escalated_at = models.DateTimeField(null=True)
    depends_on = models.ForeignKey("self", null=True, on_delete=models.PROTECT)
    completion_evidence = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    last_command_key = models.UUIDField(null=True)
    last_command_digest = models.CharField(max_length=64, default="", blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Bind command keys and expose due work through a bounded index."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_task_command_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="workflows_task_fingerprint",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_task_scope_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("open", "assigned", "in_progress", "done", "cancelled")
                ),
                name="workflows_task_state",
            ),
            models.CheckConstraint(
                condition=models.Q(kind__in=("checklist", "review", "follow_up")),
                name="workflows_task_kind",
            ),
            models.CheckConstraint(
                condition=models.Q(priority__in=("low", "normal", "high", "urgent")),
                name="workflows_task_priority",
            ),
            models.CheckConstraint(
                condition=models.Q(escalation_policy_version=1),
                name="workflows_task_escalation",
            ),
            models.CheckConstraint(
                condition=models.Q(owner_user__isnull=True) | models.Q(owner_role=""),
                name="workflows_task_one_owner",
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=("clinic", "state", "due_at"), name="workflows_task_due"
            )
        ]


class TaskComment(TenantScopedModel):
    """Append-only encrypted discussion, never copied into step input or audit."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    task = models.ForeignKey(Task, on_delete=models.PROTECT, related_name="comments")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    body = EncryptedTextField(purpose="workflows.comment.body")
    created_at = models.DateTimeField(auto_now_add=True)


class WorkflowDefinitionVersion(TenantScopedModel):
    """One immutable publication; future versions cannot rewrite existing runs."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    key = models.SlugField(max_length=64)
    version = models.PositiveIntegerField()
    steps = models.JSONField()
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    published_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    published_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Version numbers are unique inside an exact clinic/key namespace."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "key", "version"), name="workflows_definition_version"
            ),
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"),
                name="workflows_publication_command",
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="workflows_definition_digest",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="workflows_definition_scope",
            ),
        ]


class WorkflowRun(TenantScopedModel):
    """Pinned definition and reference-only context, attributed to its initiator."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    definition_version = models.ForeignKey(
        WorkflowDefinitionVersion, on_delete=models.PROTECT
    )
    started_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    context_refs = models.JSONField()
    state = models.CharField(max_length=16, default="pending")
    idempotency_key = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Replay cannot create another run or replace its pinned input."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "idempotency_key"), name="workflows_run_command_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(fingerprint__regex=r"^[0-9a-f]{64}$"),
                name="workflows_run_fingerprint",
            ),
            models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="workflows_run_scope_uniq"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "pending",
                        "running",
                        "waiting",
                        "completed",
                        "failed",
                        "cancelled",
                    )
                ),
                name="workflows_run_state",
            ),
        ]


class WorkflowStep(TenantScopedModel):
    """A durable claim, timer and receipt bound to a pinned definition position."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    run = models.ForeignKey(WorkflowRun, on_delete=models.PROTECT, related_name="steps")
    position = models.PositiveSmallIntegerField()
    state = models.CharField(max_length=16, default="pending")
    fencing_token = models.PositiveBigIntegerField(default=0)
    claim_until = models.DateTimeField(null=True)
    wake_at = models.DateTimeField(null=True)
    operation = models.ForeignKey(
        "comms.IntegrationOperation", null=True, on_delete=models.PROTECT
    )
    created_task = models.ForeignKey(Task, null=True, on_delete=models.PROTECT)
    error_code = models.CharField(max_length=24, default="", blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Only one durable step occupies each definition position in a run."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("run", "position"), name="workflows_step_position"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "pending",
                        "running",
                        "waiting",
                        "completed",
                        "failed",
                        "cancelled",
                    )
                ),
                name="workflows_step_state",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    error_code__in=(
                        "",
                        "handler_rejected",
                        "operation_failed",
                        "operation_unknown",
                    )
                ),
                name="workflows_step_error",
            ),
        ]
        indexes: ClassVar = [
            models.Index(
                fields=("state", "wake_at", "claim_until"), name="workflows_step_due"
            )
        ]

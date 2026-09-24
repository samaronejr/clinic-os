"""Tenant-scoped integration outbox records."""

import uuid
from typing import ClassVar, Final

from django.conf import settings
from django.db import models
from django.db.models.constraints import BaseConstraint

from apps.tenancy.models import TenantScopedModel

OPERATION_STATUS_VALUES: Final = [
    "pending",
    "in_progress",
    "succeeded",
    "delivered",
    "failed",
    "cancelled",
]


class IntegrationOperation(TenantScopedModel):
    """One committed external-delivery operation owned by a single tenant.

    The row stores only routing and reconciliation metadata: the operation
    identifier, the trusted actor whose authority is rechecked at execution,
    the provider/channel pair, a minimal subject reference and bounded retry
    counters. Credentials, message bodies and provider payloads are never
    persisted here.
    """

    class Channel(models.TextChoices):
        """Supported outbound delivery channels."""

        SMS = "sms", "SMS"
        EMAIL = "email", "Email"
        WHATSAPP = "whatsapp", "WhatsApp"
        VIDEO = "video", "Video"

    class Status(models.TextChoices):
        """Stored operation lifecycle states."""

        PENDING = "pending", "Pending"
        IN_PROGRESS = "in_progress", "In progress"
        SUCCEEDED = "succeeded", "Succeeded"
        DELIVERED = "delivered", "Delivered"
        FAILED = "failed", "Failed"
        CANCELLED = "cancelled", "Cancelled"

    TERMINAL_STATUSES: ClassVar[frozenset[str]] = frozenset(
        {Status.DELIVERED, Status.FAILED, Status.CANCELLED}
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.clinic", on_delete=models.PROTECT)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    channel = models.CharField(max_length=32, choices=Channel.choices)
    provider = models.CharField(max_length=64)
    subject_type = models.CharField(max_length=128)
    subject_id = models.UUIDField()
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    attempt_count = models.PositiveSmallIntegerField(default=0)
    max_attempts = models.PositiveSmallIntegerField(default=5)
    provider_reference = models.CharField(  # noqa: DJ001 - NULL keeps the
        max_length=255,  # provider-reference partial uniqueness intact
        null=True,
        blank=True,
    )
    last_callback_event_id = models.CharField(  # noqa: DJ001
        max_length=255,
        null=True,
        blank=True,
    )
    last_error = models.CharField(max_length=255, blank=True)
    idempotency_key = models.UUIDField()
    not_before = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Pin per-tenant idempotency and provider-reference uniqueness."""

        constraints: ClassVar[list[BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="comms_operation_org_idempotency_uniq",
            ),
            models.UniqueConstraint(
                fields=("provider", "provider_reference"),
                name="comms_operation_provider_reference_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=OPERATION_STATUS_VALUES),
                name="comms_operation_status_check",
            ),
            models.CheckConstraint(
                condition=models.Q(attempt_count__lte=models.F("max_attempts")),
                name="comms_operation_attempts_bounded",
            ),
        ]

    def __str__(self) -> str:
        """Return stable non-content identifiers for the operation."""
        return f"{self.id}:{self.channel}:{self.status}"


class AppointmentReminder(TenantScopedModel):
    """Immutable logistics and preference versions, never a message body."""

    operation = models.OneToOneField(
        IntegrationOperation,
        primary_key=True,
        on_delete=models.PROTECT,
        related_name="reminder",
    )
    appointment = models.ForeignKey("scheduling.Appointment", on_delete=models.PROTECT)
    preference = models.ForeignKey(
        "intake.PatientChannelPreference", on_delete=models.PROTECT
    )
    preference_version = models.PositiveIntegerField()
    contact_version = models.PositiveIntegerField()
    start_at = models.DateTimeField()
    end_at = models.DateTimeField()
    timezone = models.CharField(max_length=64)
    template_version = models.PositiveIntegerField(default=1)

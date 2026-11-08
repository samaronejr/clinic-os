"""Commercial metadata only; no patient, account credential or clinical content."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models

from apps.tenancy.models import TenantScopedModel

METERS = (
    "ai_tokens_in",
    "ai_tokens_out",
    "ai_tokens_cached",
    "ai_audio_seconds",
    "ai_cost_micros",
    "video_seconds",
    "messages",
    "storage_bytes",
    "seats",
)
CORE_FEATURES = ("clinical_core", "record_read", "record_export", "reconciliation")
STATES = (
    "trial",
    "active",
    "past_due",
    "grace",
    "restricted",
    "cancelled",
    "offboarded",
)


class Plan(models.Model):
    """Immutable packaging version; prices await the owner economics decision."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(max_length=32)
    version = models.PositiveIntegerField(default=1)
    provisional = models.BooleanField(default=True)
    monthly_price_minor = models.PositiveBigIntegerField(null=True)

    class Meta:
        """Version packages without rewriting existing subscriptions."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("code", "version"), name="subscriptions_plan_version"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    code__in=(
                        "core_operations",
                        "intelligence_usage",
                        "network_governance",
                    )
                )
                & models.Q(version__gte=1),
                name="subscriptions_plan_contract",
            ),
        ]

    def __str__(self) -> str:
        """Return the commercial package identity only."""
        return f"{self.code}:{self.version}"


class Entitlement(models.Model):
    """A closed feature or finite monthly meter limit in one packaging version."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    meter = models.CharField(max_length=32)
    enabled = models.BooleanField(default=True)
    quota = models.PositiveBigIntegerField()

    class Meta:
        """Zero is an unavailable allowance, never an unlimited allowance."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("plan", "meter"), name="subscriptions_entitlement_key"
            ),
            models.CheckConstraint(
                condition=models.Q(meter__in=METERS),
                name="subscriptions_meter_contract",
            ),
        ]

    def __str__(self) -> str:
        """Return the package and closed meter key only."""
        return f"{self.plan_id}:{self.meter}"


class Subscription(TenantScopedModel):
    """One organization's commercial lifecycle, independent of its journal books."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    plan = models.ForeignKey(Plan, on_delete=models.PROTECT)
    state = models.CharField(max_length=16, default="trial")
    revision = models.PositiveIntegerField(default=1)
    trial_ends_at = models.DateTimeField()
    grace_ends_at = models.DateTimeField(null=True)
    cancellation_at = models.DateTimeField(null=True)
    created_at = models.DateTimeField()

    class Meta:
        """Keep commercial state and tenant identity unique."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization",), name="subscriptions_one_per_org"
            ),
            models.CheckConstraint(
                condition=models.Q(state__in=STATES) & models.Q(revision__gte=1),
                name="subscriptions_state_contract",
            ),
        ]


class SubscriptionEvent(TenantScopedModel):
    """Immutable command/provider receipt; UUIDs and closed actions only."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT)
    command_id = models.UUIDField()
    action = models.CharField(max_length=32)
    plan_code = models.CharField(max_length=32, default="")
    from_state = models.CharField(max_length=16)
    to_state = models.CharField(max_length=16)
    revision = models.PositiveIntegerField()
    occurred_at = models.DateTimeField()

    class Meta:
        """Bind idempotency to the complete command, including its plan."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "command_id"), name="subscriptions_event_key"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    action__in=(
                        "trial_started",
                        "plan_changed",
                        "cancel",
                        "paid",
                        "payment_failed",
                        "advance",
                        "offboard",
                    )
                )
                & models.Q(to_state__in=STATES)
                & models.Q(from_state__in=("", *STATES)),
                name="subscriptions_event_contract",
            ),
        ]


class Seat(TenantScopedModel):
    """One retained binding to the sole identity authority, UserClinicRole."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    subscription = models.ForeignKey(Subscription, on_delete=models.PROTECT)
    membership = models.OneToOneField(
        "identity.UserClinicRole", on_delete=models.PROTECT
    )
    created_at = models.DateTimeField()


class UsageEvent(TenantScopedModel):
    """An immutable source fact; source_ref is opaque and carries no content."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meter = models.CharField(max_length=32)
    quantity = models.PositiveBigIntegerField()
    occurred_at = models.DateTimeField()
    source_ref = models.UUIDField()

    class Meta:
        """A source contributes to each meter exactly once across all periods."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "meter", "source_ref"),
                name="subscriptions_usage_source",
            ),
            models.CheckConstraint(
                condition=models.Q(meter__in=METERS) & ~models.Q(meter="seats"),
                name="subscriptions_usage_meter",
            ),
        ]


class UsageRollup(TenantScopedModel):
    """An exactly-once UTC calendar-month projection of immutable usage facts."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    meter = models.CharField(max_length=32)
    period = models.DateField()
    quantity = models.PositiveBigIntegerField()

    class Meta:
        """Late facts update their occurred-at month, not the receipt month."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("organization", "meter", "period"),
                name="subscriptions_rollup_key",
            ),
            models.CheckConstraint(
                condition=models.Q(meter__in=METERS) & ~models.Q(meter="seats"),
                name="subscriptions_rollup_meter",
            ),
        ]

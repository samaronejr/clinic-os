"""Provider capability lifecycle registry.

Platform-level (non-tenant) tables owned by ``clinic_owner``; the runtime
role ``clinic_app`` holds SELECT only. The state machine on
``CapabilityVersion.state`` is enforced by a database transition trigger
(``providers_version_transition_v1``), never by application code alone:

    researched -> selected_in_plan -> approved_to_test -> sandbox
        -> production_authorized -> activated <-> degraded
    any non-terminal state -> revoked (terminal)

Every version in ``approved_to_test`` or beyond carries an immutable
``approval`` reference to a ``CapabilityApproval`` row recorded through the
owner CLI. Nothing here authorizes spend, sandbox calls or live data; the
``activated`` state only makes a capability *eligible* for the runtime
gate in ``apps.providers.services.is_live``.
"""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.db import models


class ProviderCapability(models.Model):
    """One governed external capability, platform-wide or clinic-scoped.

    ``clinic_id`` NULL marks the platform default row; a non-null value is
    a per-clinic override that wins over the platform row for that clinic.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64)
    clinic_id = models.UUIDField(null=True, blank=True)
    record_ref = models.CharField(max_length=128, blank=True)
    description = models.TextField(blank=True)
    current_version = models.ForeignKey(
        "providers.CapabilityVersion",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Bind the per-scope key uniqueness including the NULL platform row."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.UniqueConstraint(
                fields=("key", "clinic_id"),
                nulls_distinct=False,
                name="providers_capability_key_scope_uniq",
            ),
            models.CheckConstraint(
                condition=models.Q(key__regex=r"^[a-z][a-z0-9_]{0,63}$"),
                name="providers_capability_key_format",
            ),
        ]

    def __str__(self) -> str:
        """Return the capability key."""
        return self.key


class CapabilityApproval(models.Model):
    """One immutable owner decision bound to a capability.

    Approver name and role are free text entered through the owner CLI;
    the row is never updated or deleted (immutability trigger).
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        ProviderCapability,
        on_delete=models.PROTECT,
        related_name="approvals",
    )
    approver_name = models.CharField(max_length=255)
    approver_role = models.CharField(max_length=255)
    evidence_uri = models.TextField(blank=True)
    decided_at = models.DateTimeField()

    class Meta:
        """Keep the default table name explicit."""

        db_table = "providers_capabilityapproval"

    def __str__(self) -> str:
        """Return the decision timestamp and approver role."""
        return f"{self.capability_id}:{self.decided_at}:{self.approver_role}"


class CapabilityVersion(models.Model):
    """One provider version candidate carrying the lifecycle state."""

    class State(models.TextChoices):
        """Closed lifecycle vocabulary enforced by the transition trigger."""

        RESEARCHED = "researched", "Researched"
        SELECTED_IN_PLAN = "selected_in_plan", "Selected in plan"
        APPROVED_TO_TEST = "approved_to_test", "Approved to test"
        SANDBOX = "sandbox", "Sandbox"
        PRODUCTION_AUTHORIZED = "production_authorized", "Production authorized"
        ACTIVATED = "activated", "Activated"
        DEGRADED = "degraded", "Degraded"
        REVOKED = "revoked", "Revoked"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        ProviderCapability,
        on_delete=models.PROTECT,
        related_name="versions",
    )
    provider = models.CharField(max_length=255)
    account = models.CharField(max_length=255, blank=True)
    environment = models.CharField(max_length=32, blank=True)
    api_version = models.CharField(max_length=128, blank=True)
    region = models.CharField(max_length=64, blank=True)
    retention_terms = models.TextField(blank=True)
    state = models.CharField(
        max_length=24,
        choices=State.choices,
        default=State.RESEARCHED,
    )
    approval = models.ForeignKey(
        CapabilityApproval,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        """Bind the state/approval pairing and version identity."""

        constraints: ClassVar[list[models.BaseConstraint]] = [
            models.CheckConstraint(
                condition=models.Q(
                    state__in=(
                        "researched",
                        "selected_in_plan",
                        "approved_to_test",
                        "sandbox",
                        "production_authorized",
                        "activated",
                        "degraded",
                        "revoked",
                    )
                ),
                name="providers_capabilityversion_state_vocab",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        state__in=("researched", "selected_in_plan"),
                        approval__isnull=True,
                    )
                    | models.Q(
                        state__in=(
                            "approved_to_test",
                            "sandbox",
                            "production_authorized",
                            "activated",
                            "degraded",
                            "revoked",
                        ),
                        approval__isnull=False,
                    )
                ),
                name="providers_capabilityversion_approval_pairing",
            ),
            models.UniqueConstraint(
                fields=(
                    "capability",
                    "provider",
                    "account",
                    "environment",
                    "api_version",
                    "region",
                ),
                name="providers_capabilityversion_identity_uniq",
            ),
        ]

    def __str__(self) -> str:
        """Return the provider and lifecycle state."""
        return f"{self.provider}:{self.state}"


class ActivationRecord(models.Model):
    """Immutable record of one activation decision reaching ``activated``."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        ProviderCapability,
        on_delete=models.PROTECT,
        related_name="activation_records",
    )
    version = models.ForeignKey(
        CapabilityVersion,
        on_delete=models.PROTECT,
        related_name="activation_records",
    )
    approval = models.ForeignKey(
        CapabilityApproval,
        on_delete=models.PROTECT,
        related_name="+",
    )
    activated_at = models.DateTimeField()

    def __str__(self) -> str:
        """Return the activation timestamp."""
        return f"{self.capability_id}:{self.activated_at}"


class HealthEvent(models.Model):
    """Immutable operator-recorded health or lifecycle note."""

    class Kind(models.TextChoices):
        """Closed health-event vocabulary."""

        DEGRADED = "degraded", "Degraded"
        REVOKED = "revoked", "Revoked"
        NOTE = "note", "Note"

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    capability = models.ForeignKey(
        ProviderCapability,
        on_delete=models.PROTECT,
        related_name="health_events",
    )
    version = models.ForeignKey(
        CapabilityVersion,
        on_delete=models.PROTECT,
        related_name="health_events",
    )
    approval = models.ForeignKey(
        CapabilityApproval,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="+",
    )
    kind = models.CharField(max_length=16, choices=Kind.choices)
    detail = models.TextField(blank=True)
    recorded_at = models.DateTimeField()

    def __str__(self) -> str:
        """Return the event kind and timestamp."""
        return f"{self.kind}:{self.recorded_at}"

"""Immutable order terms and append-only lifecycle receipts."""

from __future__ import annotations

import uuid
from typing import ClassVar

from django.conf import settings
from django.db import models

from apps.tenancy.models import TenantScopedModel


class ScopedRecord(TenantScopedModel):
    """An immutable clinic binding is checked independently in PostgreSQL."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    clinic = models.ForeignKey("identity.Clinic", on_delete=models.PROTECT)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        """Reuse fields without creating another tenant relation."""

        abstract = True


class LabAccount(ScopedRecord):
    """Owner-provisioned inbound identity; no clinical read or review grant."""

    principal = models.ForeignKey("identity.ServicePrincipal", on_delete=models.PROTECT)
    provider = models.CharField(max_length=32, default="synthetic-lab-v1")
    signing_key_digest = models.CharField(max_length=64)
    active = models.BooleanField(default=True)


class Order(ScopedRecord):
    """Patient, ordering clinician and external correlation never change."""

    enrollment = models.ForeignKey(
        "intake.PatientClinicEnrollment", on_delete=models.PROTECT
    )
    ordered_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    command_id = models.UUIDField()
    fingerprint = models.CharField(max_length=64)
    lab_account = models.ForeignKey(LabAccount, null=True, on_delete=models.PROTECT)
    correlation = models.UUIDField(default=uuid.uuid4, unique=True)

    class Meta:
        """A replay binds the same patient, author and catalog terms."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("clinic", "command_id"), name="orders_place_command"
            )
        ]


class OrderItem(ScopedRecord):
    """Local catalog v1 codes; no undocumented terminology or TUSS guesses."""

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="items")
    catalog_version = models.PositiveSmallIntegerField(default=1)
    exam_code = models.CharField(max_length=32)

    class Meta:
        """One requested code appears once per immutable order."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("order", "exam_code"), name="orders_item_code"
            )
        ]


class OrderEvent(ScopedRecord):
    """Draft, placement and collection are distinct, retained transitions."""

    order = models.ForeignKey(Order, on_delete=models.PROTECT, related_name="events")
    sequence = models.PositiveIntegerField()
    state = models.CharField(max_length=16)
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT)
    command_id = models.UUIDField()

    class Meta:
        """Serialize lifecycle receipts and reject altered command replays."""

        constraints: ClassVar = [
            models.UniqueConstraint(
                fields=("order", "sequence"), name="orders_event_sequence"
            ),
            models.UniqueConstraint(
                fields=("order", "command_id"), name="orders_event_command"
            ),
            models.CheckConstraint(
                condition=models.Q(
                    state__in=("draft", "placed", "collected", "in_process")
                ),
                name="orders_event_state",
            ),
        ]

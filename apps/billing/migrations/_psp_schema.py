"""Additive model state for immutable sandbox PSP requests and observations."""

from __future__ import annotations

import uuid
from typing import Final

from django.db import migrations, models
from django.utils import timezone

from apps.tenancy.fields import EncryptedTextField


def _identity() -> list[tuple[str, models.Field[object, object]]]:
    """Construct fresh field instances for each independent migration model state."""
    return [
        (
            "id",
            models.UUIDField(
                default=uuid.uuid4, editable=False, primary_key=True, serialize=False
            ),
        ),
        (
            "organization",
            models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
        ),
        ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
    ]


OPERATIONS: Final = [
    migrations.CreateModel(
        name="PspPolicy",
        fields=[
            *_identity(),
            (
                "book",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalbook"),
            ),
            ("max_installments", models.PositiveSmallIntegerField(default=1)),
            ("command_id", models.UUIDField()),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "command_id"),
                    name="billing_psp_policy_command_uniq",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="PspIntent",
        fields=[
            *_identity(),
            (
                "book",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalbook"),
            ),
            (
                "policy",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.psppolicy"),
            ),
            (
                "capability_version",
                models.ForeignKey(
                    on_delete=models.PROTECT, to="providers.capabilityversion"
                ),
            ),
            ("requested_by_id", models.UUIDField()),
            (
                "dispatch_session",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="identity.sessionrecord",
                ),
            ),
            ("provider", models.CharField(max_length=64)),
            ("kind", models.CharField(max_length=8)),
            ("target_id", models.UUIDField()),
            ("method", models.CharField(max_length=4)),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("currency", models.CharField(default="BRL", max_length=3)),
            ("installments", models.PositiveSmallIntegerField(default=1)),
            (
                "original_reference",
                models.CharField(blank=True, default="", max_length=128),
            ),
            (
                "payee_reference",
                models.CharField(blank=True, default="", max_length=32),
            ),
            (
                "original",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.pspintent",
                ),
            ),
            ("expires_at", models.DateTimeField()),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "kind", "target_id"), name="billing_psp_target_uniq"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="PspBinding",
        fields=[
            *_identity(),
            (
                "intent",
                models.OneToOneField(on_delete=models.PROTECT, to="billing.pspintent"),
            ),
            ("provider", models.CharField(max_length=64)),
            ("provider_reference", models.CharField(max_length=128)),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("provider", "provider_reference"),
                    name="billing_psp_reference_uniq",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="PspEvidence",
        fields=[
            *_identity(),
            (
                "intent",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.pspintent"),
            ),
            ("provider", models.CharField(max_length=64)),
            ("event_id", models.CharField(max_length=128)),
            ("source", models.CharField(max_length=16)),
            ("status", models.CharField(max_length=16)),
            ("decision", models.CharField(max_length=16)),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("currency", models.CharField(max_length=3)),
            ("fee_minor", models.PositiveBigIntegerField(default=0)),
            ("version", models.PositiveBigIntegerField(default=0)),
            (
                "payment_code",
                EncryptedTextField(
                    purpose="billing.psp.payment_code", null=True, blank=True
                ),
            ),
            (
                "payment_url",
                EncryptedTextField(
                    purpose="billing.psp.payment_url", null=True, blank=True
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("provider", "source", "event_id"),
                    name="billing_psp_event_uniq",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="PspReceipt",
        fields=[
            (
                "reference",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "intent",
                models.OneToOneField(on_delete=models.PROTECT, to="billing.pspintent"),
            ),
            (
                "settlement",
                models.OneToOneField(
                    on_delete=models.PROTECT, to="billing.journalsettlement"
                ),
            ),
            (
                "allocation",
                models.OneToOneField(
                    on_delete=models.PROTECT, to="billing.settlementallocation"
                ),
            ),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("issued_at", models.DateTimeField(default=timezone.now, editable=False)),
        ],
    ),
]

"""Refund, payout and counted cash-history model state."""

import uuid
from typing import Final

from django.db import migrations, models
from django.utils import timezone

OPERATIONS: Final = [
    migrations.CreateModel(
        name="Refund",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("command_id", models.UUIDField()),
            ("kind", models.CharField(default="refund", max_length=16)),
            ("state", models.CharField(default="requested", max_length=16)),
            ("requested_by_id", models.UUIDField()),
            ("approved_by_id", models.UUIDField(blank=True, null=True)),
            ("evidence_reference", models.UUIDField(blank=True, null=True)),
            ("repasse_reversed_minor", models.PositiveBigIntegerField(default=0)),
            ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "book",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalbook"),
            ),
            (
                "allocation",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.settlementallocation",
                ),
            ),
            (
                "settlement",
                models.ForeignKey(
                    on_delete=models.PROTECT, to="billing.journalsettlement"
                ),
            ),
            (
                "approval_session",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="identity.sessionrecord",
                ),
            ),
            (
                "approval_entry",
                models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    related_name="+",
                    to="billing.journalentry",
                ),
            ),
            (
                "repasse_entry",
                models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    related_name="+",
                    to="billing.journalentry",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "command_id"), name="billing_refund_command_uniq"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="Payout",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("command_id", models.UUIDField()),
            ("state", models.CharField(default="requested", max_length=16)),
            ("requested_by_id", models.UUIDField()),
            ("approved_by_id", models.UUIDField(blank=True, null=True)),
            ("evidence_reference", models.UUIDField(blank=True, null=True)),
            ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "book",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalbook"),
            ),
            (
                "practitioner",
                models.ForeignKey(on_delete=models.PROTECT, to="identity.user"),
            ),
            (
                "approval_session",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="identity.sessionrecord",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "command_id"), name="billing_payout_command_uniq"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="CashSession",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("command_id", models.UUIDField()),
            ("last_command_id", models.UUIDField()),
            ("state", models.CharField(default="open", max_length=8)),
            ("counted_minor", models.PositiveBigIntegerField()),
            ("expected_minor", models.BigIntegerField(default=0)),
            ("revision", models.PositiveIntegerField(default=1)),
            ("reason", models.CharField(default="", max_length=16)),
            ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "book",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalbook"),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    condition=models.Q(state="open"),
                    fields=("book",),
                    name="billing_cash_one_open",
                ),
                models.UniqueConstraint(
                    fields=("book", "command_id"), name="billing_cash_command_uniq"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="CashSessionEvent",
        fields=[
            (
                "id",
                models.UUIDField(
                    default=uuid.uuid4,
                    editable=False,
                    primary_key=True,
                    serialize=False,
                ),
            ),
            ("revision", models.PositiveIntegerField()),
            ("command_id", models.UUIDField()),
            ("state", models.CharField(max_length=16)),
            ("counted_minor", models.PositiveBigIntegerField()),
            ("expected_minor", models.BigIntegerField()),
            ("reason", models.CharField(default="", max_length=16)),
            ("actor_id", models.UUIDField()),
            ("verification_session_id", models.UUIDField(blank=True, null=True)),
            ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "book",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalbook"),
            ),
            (
                "session",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.cashsession"),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("session", "revision"), name="billing_cash_event_uniq"
                ),
                models.UniqueConstraint(
                    fields=("book", "command_id"),
                    name="billing_cash_event_command_uniq",
                ),
            ]
        },
    ),
]

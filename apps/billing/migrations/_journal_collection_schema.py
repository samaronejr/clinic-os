"""Frozen terms, verified collections and allocation model state."""

import uuid
from typing import Final

from django.db import migrations, models
from django.utils import timezone

OPERATIONS: Final = [
    migrations.CreateModel(
        name="PractitionerPayoutRule",
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
            ("kind", models.CharField(max_length=8)),
            ("value", models.PositiveBigIntegerField()),
            ("command_id", models.UUIDField()),
            (
                "policy_version",
                models.CharField(default="completion-net-v1", max_length=32),
            ),
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
                "service_type",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="scheduling.servicetype",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "command_id"), name="billing_rule_command_uniq"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="InvoiceTerms",
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
            ("payer", models.CharField(default="patient", max_length=8)),
            ("discount_minor", models.PositiveBigIntegerField(default=0)),
            ("payer_adjustment_minor", models.PositiveBigIntegerField(default=0)),
            ("net_minor", models.PositiveBigIntegerField()),
            ("completion_evidence", models.UUIDField(blank=True, null=True)),
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
                "invoice",
                models.OneToOneField(on_delete=models.PROTECT, to="billing.invoice"),
            ),
            (
                "payout_rule",
                models.ForeignKey(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.practitionerpayoutrule",
                ),
            ),
            (
                "completion_entry",
                models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.journalentry",
                ),
            ),
        ],
    ),
    migrations.CreateModel(
        name="JournalSettlement",
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
            ("kind", models.CharField(max_length=16)),
            ("account", models.CharField(max_length=16)),
            ("evidence_reference", models.UUIDField()),
            ("command_id", models.UUIDField()),
            ("state", models.CharField(default="observed", max_length=16)),
            ("verified_by_id", models.UUIDField(blank=True, null=True)),
            ("verified_at", models.DateTimeField(blank=True, null=True)),
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
                "legacy_settlement",
                models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.settlement",
                ),
            ),
            (
                "entry",
                models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.journalentry",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "command_id"),
                    name="billing_settlement_command_uniq",
                ),
                models.UniqueConstraint(
                    fields=("book", "evidence_reference"),
                    name="billing_settlement_evidence_uniq",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="SettlementAllocation",
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
            ("repasse_minor", models.PositiveBigIntegerField(default=0)),
            ("repasse_side", models.CharField(default="credit", max_length=6)),
            ("command_id", models.UUIDField()),
            ("group_id", models.UUIDField(blank=True, null=True)),
            (
                "group_fingerprint",
                models.BinaryField(blank=True, max_length=32, null=True),
            ),
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
                "settlement",
                models.ForeignKey(
                    on_delete=models.PROTECT, to="billing.journalsettlement"
                ),
            ),
            (
                "terms",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.invoiceterms"),
            ),
            (
                "entry",
                models.OneToOneField(
                    blank=True,
                    null=True,
                    on_delete=models.PROTECT,
                    to="billing.journalentry",
                ),
            ),
        ],
        options={
            "constraints": [
                models.UniqueConstraint(
                    fields=("book", "command_id"),
                    name="billing_allocation_command_uniq",
                ),
                models.UniqueConstraint(
                    condition=models.Q(group_id__isnull=False),
                    fields=("book", "group_id", "terms"),
                    name="billing_allocation_group_terms_uniq",
                ),
            ]
        },
    ),
]

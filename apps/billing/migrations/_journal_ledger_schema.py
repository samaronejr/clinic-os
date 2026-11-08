"""Book, chart and sealed journal model state."""

import uuid
from typing import Final

from django.db import migrations, models
from django.utils import timezone

OPERATIONS: Final = [
    migrations.CreateModel(
        name="JournalBook",
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
            (
                "policy_version",
                models.CharField(default="completion-net-v1", max_length=32),
            ),
            ("currency", models.CharField(default="BRL", max_length=3)),
            ("created_at", models.DateTimeField(default=timezone.now, editable=False)),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "clinic",
                models.OneToOneField(on_delete=models.PROTECT, to="identity.clinic"),
            ),
        ],
    ),
    migrations.CreateModel(
        name="Account",
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
            ("code", models.CharField(max_length=32)),
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
                    fields=("book", "code"), name="billing_account_book_code_uniq"
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="JournalEntry",
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
            ("source_kind", models.CharField(max_length=32)),
            ("source_id", models.UUIDField()),
            ("content_hash", models.BinaryField(max_length=32)),
            ("state", models.CharField(default="proposed", max_length=16)),
            ("posted_at", models.DateTimeField(blank=True, null=True)),
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
                "reversal_of",
                models.ForeignKey(
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
                    fields=("book", "source_kind", "source_id"),
                    name="billing_entry_fact_uniq",
                ),
            ]
        },
    ),
    migrations.CreateModel(
        name="JournalLine",
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
            ("side", models.CharField(max_length=6)),
            ("amount_minor", models.PositiveBigIntegerField()),
            ("practitioner_id", models.UUIDField(blank=True, null=True)),
            ("owner_id", models.UUIDField(blank=True, null=True)),
            ("due_on", models.DateField(blank=True, null=True)),
            (
                "organization",
                models.ForeignKey(on_delete=models.CASCADE, to="identity.organization"),
            ),
            (
                "entry",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.journalentry"),
            ),
            (
                "account",
                models.ForeignKey(on_delete=models.PROTECT, to="billing.account"),
            ),
        ],
    ),
]

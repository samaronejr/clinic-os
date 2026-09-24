"""Persist exact invoice terms and their append-only financial lineage."""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):
    """Create billing records before installing their mandatory policies."""

    initial = True

    dependencies: ClassVar = [
        ("ehr", "0008_finalization_policy"),
        ("identity", "0007_physician_verification_policy"),
        ("intake", "0009_teleconsult_operation"),
        ("scheduling", "0004_waitlistentry_waitlistoffer_and_more"),
    ]

    operations: ClassVar = [
        migrations.CreateModel(
            name="Invoice",
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
                    "reference",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(default="BRL", max_length=3)),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("draft", "Draft"),
                            ("open", "Open"),
                            ("paid", "Paid"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="draft",
                        max_length=16,
                    ),
                ),
                ("revision", models.PositiveIntegerField(default=1)),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("issued_at", models.DateTimeField(blank=True, null=True)),
                ("released_at", models.DateTimeField(blank=True, null=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.appointment",
                    ),
                ),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "encounter",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="ehr.encounter",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="InvoiceRevision",
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
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(max_length=3)),
                ("reference", models.UUIDField()),
                ("actor_id", models.UUIDField()),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                (
                    "invoice",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.invoice",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Settlement",
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
                ("confirmation_reference", models.UUIDField()),
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(default="BRL", max_length=3)),
                ("confirmed_by_id", models.UUIDField()),
                (
                    "confirmed_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                (
                    "invoice",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.invoice",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Receipt",
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
                    "reference",
                    models.UUIDField(default=uuid.uuid4, editable=False, unique=True),
                ),
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(max_length=3)),
                (
                    "issued_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                (
                    "invoice",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.invoice",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "settlement",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.settlement",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("amount_minor__gt", 0), ("currency", "BRL"), ("revision__gte", 1)
                ),
                name="billing_invoice_terms",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("issued_at__isnull", True), ("state", "draft")),
                    models.Q(
                        ("issued_at__isnull", False), ("state__in", ["open", "paid"])
                    ),
                    ("state", "cancelled"),
                    _connector="OR",
                ),
                name="billing_invoice_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoice",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("released_at__isnull", True),
                    ("issued_at__isnull", False),
                    _connector="OR",
                ),
                name="billing_invoice_release",
            ),
        ),
        migrations.AddConstraint(
            model_name="invoicerevision",
            constraint=models.UniqueConstraint(
                fields=("invoice", "revision"), name="billing_revision_unique"
            ),
        ),
        migrations.AddConstraint(
            model_name="settlement",
            constraint=models.UniqueConstraint(
                fields=("organization", "confirmation_reference"),
                name="billing_confirmation_unique",
            ),
        ),
    ]

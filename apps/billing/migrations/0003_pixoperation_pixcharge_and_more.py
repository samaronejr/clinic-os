"""Retain exact synthetic PIX requests and verified immutable results."""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING, ClassVar

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models

if TYPE_CHECKING:
    from django.db.backends.base.schema import BaseDatabaseSchemaEditor
    from django.db.migrations.state import StateApps


def finish_foreign_keys(
    _apps: StateApps, schema_editor: BaseDatabaseSchemaEditor
) -> None:
    """Validate deferred FKs as owner, restoring FORCE within this transaction."""
    for statement in schema_editor.deferred_sql:
        schema_editor.execute(statement)
    schema_editor.deferred_sql.clear()
    schema_editor.execute(
        "ALTER TABLE clinic_app.billing_invoice FORCE ROW LEVEL SECURITY"
    )


class Migration(migrations.Migration):
    """Create operations separately from their exact-scope policy migration."""

    dependencies: ClassVar = [
        ("billing", "0002_invoice_policy"),
        ("identity", "0007_physician_verification_policy"),
    ]

    operations: ClassVar = [
        migrations.RunSQL(
            "ALTER TABLE clinic_app.billing_invoice NO FORCE ROW LEVEL SECURITY",
            "ALTER TABLE clinic_app.billing_invoice FORCE ROW LEVEL SECURITY",
        ),
        migrations.CreateModel(
            name="PixOperation",
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
                ("invoice_reference", models.UUIDField()),
                ("amount_minor", models.PositiveBigIntegerField()),
                ("currency", models.CharField(max_length=3)),
                (
                    "provider",
                    models.CharField(default="synthetic-pix-v1", max_length=64),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("expires_at", models.DateTimeField()),
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
                (
                    "previous",
                    models.OneToOneField(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="billing.pixoperation",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="PixCharge",
            fields=[
                (
                    "operation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        primary_key=True,
                        serialize=False,
                        to="billing.pixoperation",
                    ),
                ),
                ("provider_reference", models.CharField(max_length=128, unique=True)),
                ("copy_code", models.TextField()),
                ("qr_base64", models.TextField()),
                ("synthetic", models.BooleanField(default=True)),
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
        migrations.AddConstraint(
            model_name="pixoperation",
            constraint=models.UniqueConstraint(
                condition=models.Q(("previous__isnull", True)),
                fields=("invoice",),
                name="billing_pix_one_root",
            ),
        ),
        migrations.AddConstraint(
            model_name="pixoperation",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("amount_minor__gt", 0),
                    ("currency", "BRL"),
                    ("provider", "synthetic-pix-v1"),
                    ("expires_at__gt", models.F("created_at")),
                ),
                name="billing_pix_terms",
            ),
        ),
        migrations.AddConstraint(
            model_name="pixcharge",
            constraint=models.CheckConstraint(
                condition=models.Q(("synthetic", True)), name="billing_pix_synthetic"
            ),
        ),
        migrations.RunPython(finish_foreign_keys, migrations.RunPython.noop),
    ]

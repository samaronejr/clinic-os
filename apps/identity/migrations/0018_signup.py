"""Create protected self-signup captures without granting runtime authority."""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

import apps.tenancy.fields
from apps.identity.migrations._signup_sql import REMOVE_SIGNUP_SQL, SIGNUP_SQL


class Migration(migrations.Migration):
    """Install protected-from-creation capture and the narrow creation resolver."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0017_workflow_bundle_subtractions"),
        ("tenancy", "0007_blind_indexes"),
        ("audit", "0005_clinic_metadata_v2"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.AddField(
            model_name="organization",
            name="status",
            field=models.CharField(
                choices=[
                    ("active", "Active"),
                    ("pending_activation", "Pending activation"),
                ],
                default="active",
                max_length=20,
            ),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=models.Q(status__in=("active", "pending_activation")),
                name="identity_organization_status",
            ),
        ),
        migrations.AddConstraint(
            model_name="organization",
            constraint=models.CheckConstraint(
                condition=~models.Q(status="pending_activation")
                | models.Q(name="", cnpj=""),
                name="identity_pending_capture_protected",
            ),
        ),
        migrations.CreateModel(
            name="SignupRegistration",
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
                ("idempotency_key", models.UUIDField(unique=True)),
                (
                    "payload",
                    apps.tenancy.fields.EncryptedJSONField(
                        purpose="identity.signup.capture"
                    ),
                ),
                ("payload_fingerprint", models.BinaryField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.organization",
                    ),
                ),
                (
                    "first_admin",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.RunSQL(SIGNUP_SQL, REMOVE_SIGNUP_SQL),
    ]

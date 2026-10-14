"""Create protected public credentials with the exact ACLs in one DDL transaction."""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._passkey_sql import PASSKEY_SQL
from apps.tenancy.fields import EncryptedTextField


class Migration(migrations.Migration):
    """Add bytea protection from creation; no plaintext conversion or backfill."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0031_cut2_current_membership"),
    ]
    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="Passkey",
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
                ("credential_id", models.BinaryField(unique=True)),
                ("public_key", models.BinaryField()),
                ("sign_count", models.PositiveBigIntegerField(default=0)),
                ("transports", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("last_used", models.DateTimeField(blank=True, null=True)),
                (
                    "nickname",
                    EncryptedTextField(
                        blank=True, null=True, purpose="identity.passkey.nickname"
                    ),
                ),
                ("removed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.organization",
                    ),
                ),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(sign_count__lte=4294967295),
                        name="identity_passkey_counter_uint32",
                    ),
                ],
            },
        ),
        migrations.RunSQL(PASSKEY_SQL),
    ]

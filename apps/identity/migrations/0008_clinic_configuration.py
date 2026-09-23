"""Store immutable clinic configuration snapshots."""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation


class Migration(migrations.Migration):
    """Create the bounded fields; the following migration installs FORCE RLS."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0007_physician_verification_policy"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="ClinicConfiguration",
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
                ("version", models.PositiveIntegerField()),
                ("display_name", models.CharField(max_length=120)),
                ("contact_email", models.EmailField(blank=True, max_length=254)),
                ("contact_phone", models.CharField(blank=True, max_length=20)),
                (
                    "brand_token",
                    models.CharField(
                        choices=[("navy", "Azul"), ("teal", "Verde")],
                        default="navy",
                        max_length=8,
                    ),
                ),
                ("reminder_hours", models.PositiveSmallIntegerField(default=24)),
                ("logo_png", models.BinaryField(blank=True, default=bytes)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
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
                    "published_by",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "version"),
                        name="identity_config_version_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            ("version__gte", 1),
                            ("reminder_hours__in", (1, 2, 6, 12, 24, 48, 72)),
                            ("brand_token__in", ("navy", "teal")),
                        ),
                        name="identity_config_bounded_values",
                    ),
                ],
            },
        ),
    ]

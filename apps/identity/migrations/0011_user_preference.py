"""Store per-user display preferences, readable and editable only by the user."""

from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._user_preference_sql import FORWARD_SQL, REVERSE_SQL


class Migration(migrations.Migration):
    """Create the table, then bind every row to the transaction's user GUC."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0010_configuration_reminder_schedule"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="UserPreference",
            fields=[
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        primary_key=True,
                        serialize=False,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "theme",
                    models.CharField(
                        choices=[("light", "Light"), ("dark", "Dark")],
                        default="light",
                        max_length=16,
                    ),
                ),
                (
                    "density",
                    models.CharField(
                        choices=[
                            ("comfortable", "Comfortable"),
                            ("compact", "Compact"),
                        ],
                        default="comfortable",
                        max_length=16,
                    ),
                ),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("theme__in", ("light", "dark")),
                            ("density__in", ("comfortable", "compact")),
                        ),
                        name="identity_userpreference_closed_values",
                    )
                ],
            },
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]

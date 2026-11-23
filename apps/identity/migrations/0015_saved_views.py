"""Store per-user saved workspace views, readable and archivable only by the user.

A row is visible only to its user, in the current tenant, while the user still
holds a role in the row's clinic. The runtime role inserts and archives (the
``archived_at`` column) and never deletes; parameters are closed slugs.
"""

import uuid
from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.identity.migrations._saved_views_sql import FORWARD_SQL, REVERSE_SQL


class Migration(migrations.Migration):
    """Create the table, then bind every row to its user, tenant and clinic role."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("identity", "0014_service_principals"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.CreateModel(
            name="SavedView",
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
                ("destination", models.CharField(max_length=32)),
                ("params", models.JSONField(default=dict)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("archived_at", models.DateTimeField(blank=True, null=True)),
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
                        on_delete=django.db.models.deletion.CASCADE,
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
                        condition=models.Q(
                            ("destination__regex", "^[a-z][a-z-]{0,31}$")
                        ),
                        name="identity_savedview_destination_slug",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(("archived_at__isnull", True)),
                        fields=("user", "clinic", "destination", "params"),
                        name="identity_savedview_active_uniq",
                    ),
                ],
            },
        ),
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]

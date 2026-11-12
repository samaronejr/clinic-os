"""Show delegate reads in the represented patient's access log (DG-R1)."""

from __future__ import annotations

import uuid
from typing import Any, ClassVar

import django.db.models.deletion
from django.db import migrations, models

from apps.identity.migrations._delegate_access_sql import REVERSE_SQL, SQL
from apps.interop.migrations import _export_v5_sql, _import_activity_v3_sql


class Migration(migrations.Migration):
    """Create the delegate access record, its writer and the widened log."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0051_enterprise_governance"),
        ("intake", "0029_membership_payer"),
    ]

    operations: ClassVar[list[Any]] = [
        migrations.CreateModel(
            name="DelegateAccessEvent",
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
                    "record_type",
                    models.CharField(
                        choices=[
                            ("clinical_record", "Clinical Record"),
                            ("result", "Result"),
                        ],
                        max_length=16,
                    ),
                ),
                ("accessed_at", models.DateTimeField()),
                (
                    "clinic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.clinic",
                    ),
                ),
                (
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientclinicenrollment",
                    ),
                ),
                (
                    "grant",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="identity.patientdelegategrant",
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
            options={
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(
                            ("record_type__in", ("clinical_record", "result"))
                        ),
                        name="identity_delegate_access_record_type",
                    )
                ],
            },
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
        migrations.RunSQL(
            _import_activity_v3_sql.FORWARD, _import_activity_v3_sql.REVERSE
        ),
        migrations.RunSQL(_export_v5_sql.FORWARD, _export_v5_sql.REVERSE),
    ]

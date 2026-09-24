"""Install durable reminder snapshots and the atomic appointment hook."""

from collections.abc import Sequence
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.comms.migrations._reminder_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Preserve existing RLS and grant only immutable reminder reads to staff."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("comms", "0001_integration_operation"),
        ("identity", "0005_clinic_timezone"),
        (
            "intake",
            "0006_remove_patientchannelpreference_intake_preference_purpose_check_and_more",
        ),
        ("scheduling", "0004_waitlistentry_waitlistoffer_and_more"),
    ]

    operations: ClassVar[Sequence[Operation]] = [
        migrations.AddField(
            model_name="integrationoperation",
            name="not_before",
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.CreateModel(
            name="AppointmentReminder",
            fields=[
                (
                    "operation",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        primary_key=True,
                        related_name="reminder",
                        serialize=False,
                        to="comms.integrationoperation",
                    ),
                ),
                ("preference_version", models.PositiveIntegerField()),
                ("contact_version", models.PositiveIntegerField()),
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("timezone", models.CharField(max_length=64)),
                ("template_version", models.PositiveIntegerField(default=1)),
                (
                    "appointment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.appointment",
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
                    "preference",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientchannelpreference",
                    ),
                ),
            ],
            options={
                "abstract": False,
            },
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]

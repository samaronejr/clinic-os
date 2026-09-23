"""Create the tenant-scoped integration outbox with hardened scope resolvers."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.comms.migrations._operation_sql import (
    INTEGRITY_SQL,
    RESOLVER_SQL,
    REVERSE_INTEGRITY_SQL,
    REVERSE_RESOLVER_SQL,
    REVERSE_RUNTIME_ACL_SQL,
    RUNTIME_ACL_SQL,
)
from apps.comms.rls import (
    COMMS_RLS_TARGETS,
    apply_comms_rls,
    remove_comms_rls,
)


class Migration(migrations.Migration):
    """Install the first reversible comms schema after current leaves."""

    initial = True
    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        ("tenancy", "0002_rls_and_resolvers"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="IntegrationOperation",
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
                    "channel",
                    models.CharField(
                        choices=[
                            ("sms", "SMS"),
                            ("email", "Email"),
                            ("whatsapp", "WhatsApp"),
                        ],
                        max_length=32,
                    ),
                ),
                ("provider", models.CharField(max_length=64)),
                ("subject_type", models.CharField(max_length=128)),
                ("subject_id", models.UUIDField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("pending", "Pending"),
                            ("in_progress", "In progress"),
                            ("succeeded", "Succeeded"),
                            ("delivered", "Delivered"),
                            ("failed", "Failed"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                ("attempt_count", models.PositiveSmallIntegerField(default=0)),
                ("max_attempts", models.PositiveSmallIntegerField(default=5)),
                (
                    "provider_reference",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                (
                    "last_callback_event_id",
                    models.CharField(blank=True, max_length=255, null=True),
                ),
                ("last_error", models.CharField(blank=True, max_length=255)),
                ("idempotency_key", models.UUIDField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "actor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "idempotency_key"),
                        name="comms_operation_org_idempotency_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("provider", "provider_reference"),
                        name="comms_operation_provider_reference_uniq",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            status__in=[
                                "pending",
                                "in_progress",
                                "succeeded",
                                "delivered",
                                "failed",
                                "cancelled",
                            ]
                        ),
                        name="comms_operation_status_check",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(attempt_count__lte=models.F("max_attempts")),
                        name="comms_operation_attempts_bounded",
                    ),
                ]
            },
        ),
        migrations.RunSQL(sql=INTEGRITY_SQL, reverse_sql=REVERSE_INTEGRITY_SQL),
        *(
            migrations.RunSQL(
                sql=apply_comms_rls(table, tenant_column),
                reverse_sql=remove_comms_rls(table, tenant_column),
            )
            for table, tenant_column in sorted(COMMS_RLS_TARGETS)
        ),
        migrations.RunSQL(sql=RESOLVER_SQL, reverse_sql=REVERSE_RESOLVER_SQL),
        migrations.RunSQL(sql=RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
    ]

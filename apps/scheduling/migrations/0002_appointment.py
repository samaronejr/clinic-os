"""Create appointment bookings with immediate scheduled overlap protection."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.conf import settings
from django.contrib.postgres.constraints import ExclusionConstraint
from django.contrib.postgres.fields import (
    DateTimeRangeField,
    RangeBoundary,
    RangeOperators,
)
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.scheduling.migrations._appointment_sql import (
    INDEX_SQL,
    INTEGRITY_SQL,
    REVERSE_INDEX_SQL,
    REVERSE_INTEGRITY_SQL,
    REVERSE_RUNTIME_ACL_SQL,
    RUNTIME_ACL_SQL,
)
from apps.scheduling.rls import (
    APPOINTMENT_RLS_TARGETS,
    apply_scheduling_rls,
    remove_scheduling_rls,
)


class Migration(migrations.Migration):
    """Install appointments after every current schema and audit leaf."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0005_clinic_metadata_v2"),
        ("identity", "0005_clinic_timezone"),
        ("intake", "0001_patient_and_enrollment"),
        ("scheduling", "0001_availability_block"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="Appointment",
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
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("idempotency_key", models.UUIDField()),
                (
                    "create_fingerprint",
                    models.BinaryField(editable=False, max_length=32),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("scheduled", "Scheduled"),
                            ("cancelled", "Cancelled"),
                        ],
                        default="scheduled",
                        max_length=10,
                    ),
                ),
                (
                    "cancellation_reason",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("patient_request", "Patient request"),
                            ("clinic_request", "Clinic request"),
                            (
                                "practitioner_unavailable",
                                "Practitioner unavailable",
                            ),
                            ("duplicate", "Duplicate"),
                            ("other", "Other"),
                        ],
                        max_length=32,
                        null=True,
                    ),
                ),
                ("cancelled_at", models.DateTimeField(blank=True, null=True)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
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
                    "patient",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patient",
                    ),
                ),
                (
                    "practitioner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("organization", "idempotency_key"),
                        name="scheduling_appointment_org_idempotency_uniq",
                    ),
                    models.UniqueConstraint(
                        fields=("organization", "clinic", "id"),
                        name="scheduling_appointment_org_clinic_id_uniq",
                    ),
                    ExclusionConstraint(
                        name=("scheduling_appointment_scheduled_practitioner_excl"),
                        expressions=(
                            ("practitioner", RangeOperators.EQUAL),
                            (
                                models.Func(
                                    "start_at",
                                    "end_at",
                                    RangeBoundary(),
                                    function="TSTZRANGE",
                                    output_field=DateTimeRangeField(),
                                ),
                                RangeOperators.OVERLAPS,
                            ),
                        ),
                        condition=models.Q(status="scheduled"),
                    ),
                    ExclusionConstraint(
                        name="scheduling_appointment_scheduled_patient_excl",
                        expressions=(
                            ("patient", RangeOperators.EQUAL),
                            (
                                models.Func(
                                    "start_at",
                                    "end_at",
                                    RangeBoundary(),
                                    function="TSTZRANGE",
                                    output_field=DateTimeRangeField(),
                                ),
                                RangeOperators.OVERLAPS,
                            ),
                        ),
                        condition=models.Q(status="scheduled"),
                    ),
                ]
            },
        ),
        migrations.RunSQL(sql=INTEGRITY_SQL, reverse_sql=REVERSE_INTEGRITY_SQL),
        *(
            migrations.RunSQL(
                sql=apply_scheduling_rls(table, tenant_column),
                reverse_sql=remove_scheduling_rls(table, tenant_column),
            )
            for table, tenant_column in sorted(APPOINTMENT_RLS_TARGETS)
        ),
        migrations.RunSQL(sql=RUNTIME_ACL_SQL, reverse_sql=REVERSE_RUNTIME_ACL_SQL),
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(sql=INDEX_SQL, reverse_sql=REVERSE_INDEX_SQL),
            ],
            state_operations=[
                migrations.AddIndex(
                    model_name="appointment",
                    index=models.Index(
                        fields=("clinic", "start_at"),
                        name="sched_appt_clinic_start_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="appointment",
                    index=models.Index(
                        fields=("practitioner", "start_at"),
                        name="sched_appt_pract_start_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="appointment",
                    index=models.Index(
                        fields=("patient", "start_at"),
                        name="sched_appt_patient_start_idx",
                    ),
                ),
                migrations.AddIndex(
                    model_name="appointment",
                    index=models.Index(
                        fields=("status", "start_at"),
                        name="sched_appt_status_start_idx",
                    ),
                ),
            ],
        ),
    ]

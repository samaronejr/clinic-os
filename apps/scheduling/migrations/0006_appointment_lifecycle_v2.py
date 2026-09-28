"""Appointment lifecycle v2 (todo 22, D-9): enum widening + constraint rewrite.

One migration widens ``status`` (10 -> 16), rewrites both occupancy exclusions to
{held, scheduled, arrived, in_progress}, installs the transition trigger, hold
expiry, series tables and the EHR arrival predicate. Rollback is a restore once
v2 states exist (the reverse refuses a populated lifecycle).
"""

import uuid
from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.ehr.migrations._arrival_sql import REVERSE_SQL as EHR_REVERSE_SQL
from apps.ehr.migrations._arrival_sql import SQL as EHR_SQL
from apps.scheduling.migrations._lifecycle_sql import (
    ACL_SQL,
    CLOCK_SQL,
    CONSTRAINT_SQL,
    CREATE_REMINDER_TRIGGER_SQL,
    DROP_REMINDER_TRIGGER_SQL,
    GUARD_SQL,
    REVERSE_ACL_SQL,
    REVERSE_CLOCK_SQL,
    REVERSE_CONSTRAINT_SQL,
    REVERSE_GUARD_SQL,
)
from apps.scheduling.rls import (
    LIFECYCLE_RLS_TARGETS,
    apply_scheduling_rls,
    remove_scheduling_rls,
)


class Migration(migrations.Migration):
    """Widen the lifecycle in one rehearsed schema transaction."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("comms", "0005_operation_state_counts"),
        ("ehr", "0009_protected_fields"),
        ("identity", "0014_scheduling_policy"),
        ("intake", "0011_protected_fields"),
        ("scheduling", "0005_resources_templates"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="AppointmentSeries",
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
                    "frequency",
                    models.CharField(
                        choices=[
                            ("weekly", "Weekly"),
                            ("biweekly", "Every two weeks"),
                            ("monthly", "Monthly"),
                        ],
                        max_length=16,
                    ),
                ),
                ("weekday", models.PositiveSmallIntegerField()),
                ("month_week", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("first_date", models.DateField()),
                ("start_local", models.TimeField()),
                ("end_local", models.TimeField()),
                ("timezone", models.CharField(max_length=63)),
                ("count", models.PositiveSmallIntegerField(blank=True, null=True)),
                ("until", models.DateField(blank=True, null=True)),
                ("idempotency_key", models.UUIDField()),
                ("create_fingerprint", models.BinaryField(max_length=32)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        migrations.CreateModel(
            name="AppointmentTransition",
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
                ("from_status", models.CharField(blank=True, max_length=16)),
                ("to_status", models.CharField(max_length=16)),
                ("revision", models.PositiveIntegerField()),
                ("command_id", models.UUIDField(blank=True, null=True)),
                ("actor_kind", models.CharField(max_length=8)),
                ("occurred_at", models.DateTimeField()),
            ],
        ),
        migrations.CreateModel(
            name="SeriesException",
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
                ("occurrence_index", models.PositiveSmallIntegerField()),
                (
                    "scope",
                    models.CharField(
                        choices=[
                            ("this", "This occurrence"),
                            ("future", "This and future occurrences"),
                        ],
                        max_length=8,
                    ),
                ),
                (
                    "kind",
                    models.CharField(
                        choices=[("cancelled", "Cancelled"), ("moved", "Moved")],
                        max_length=16,
                    ),
                ),
                ("start_local", models.TimeField(blank=True, null=True)),
                ("command_id", models.UUIDField()),
                ("affected_count", models.PositiveSmallIntegerField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
            ],
        ),
        # Index order decides which exclusion reports first; the buffer
        # exclusion is recreated last so the named slot exclusions keep
        # precedence exactly as before the rewrite.
        migrations.RemoveConstraint(
            model_name="appointment",
            name="scheduling_z_buffer_practitioner_excl",
        ),
        migrations.RemoveConstraint(
            model_name="appointment",
            name="scheduling_appointment_scheduled_practitioner_excl",
        ),
        migrations.RemoveConstraint(
            model_name="appointment",
            name="scheduling_appointment_scheduled_patient_excl",
        ),
        migrations.AddField(
            model_name="appointment",
            name="authorization_reference",
            field=models.CharField(
                blank=True, db_default="", default="", max_length=64
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="hold_expires_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="last_command_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="payer_membership_id",
            field=models.UUIDField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="revision",
            field=models.PositiveIntegerField(db_default=1, default=1, editable=False),
        ),
        migrations.AddField(
            model_name="appointment",
            name="series_index",
            field=models.PositiveSmallIntegerField(blank=True, null=True),
        ),
        migrations.AddField(
            model_name="appointment",
            name="transitioned_at",
            field=models.DateTimeField(blank=True, editable=False, null=True),
        ),
        migrations.RunSQL(DROP_REMINDER_TRIGGER_SQL, CREATE_REMINDER_TRIGGER_SQL),
        migrations.AlterField(
            model_name="appointment",
            name="status",
            field=models.CharField(
                choices=[
                    ("scheduled", "Scheduled"),
                    ("cancelled", "Cancelled"),
                    ("requested", "Requested"),
                    ("held", "Held"),
                    ("arrived", "Arrived"),
                    ("in_progress", "In progress"),
                    ("completed", "Completed"),
                    ("expired", "Expired"),
                    ("no_show", "No-show"),
                ],
                default="scheduled",
                max_length=16,
            ),
        ),
        migrations.RunSQL(CREATE_REMINDER_TRIGGER_SQL, DROP_REMINDER_TRIGGER_SQL),
        migrations.AddField(
            model_name="appointmentseries",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="appointmentseries",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="appointmentseries",
            name="patient",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
            ),
        ),
        migrations.AddField(
            model_name="appointmentseries",
            name="practitioner",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to=settings.AUTH_USER_MODEL
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="series",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name="occurrences",
                to="scheduling.appointmentseries",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(
                    ("status__in", ["held", "scheduled", "arrived", "in_progress"])
                ),
                expressions=(
                    ("practitioner", "="),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            django.contrib.postgres.fields.ranges.RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=django.contrib.postgres.fields.ranges.DateTimeRangeField(),
                        ),
                        "&&",
                    ),
                ),
                name="scheduling_appointment_scheduled_practitioner_excl",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(
                    ("status__in", ["held", "scheduled", "arrived", "in_progress"])
                ),
                expressions=(
                    ("patient", "="),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            django.contrib.postgres.fields.ranges.RangeBoundary(),
                            function="TSTZRANGE",
                            output_field=django.contrib.postgres.fields.ranges.DateTimeRangeField(),
                        ),
                        "&&",
                    ),
                ),
                name="scheduling_appointment_scheduled_patient_excl",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(
                    ("status__in", ["held", "scheduled", "arrived", "in_progress"])
                ),
                expressions=(
                    ("practitioner", "="),
                    (
                        models.Func(
                            "start_at",
                            "end_at",
                            "buffer_before",
                            "buffer_after",
                            function="clinic_app.scheduling_effective_interval",
                            output_field=django.contrib.postgres.fields.ranges.DateTimeRangeField(),
                        ),
                        "&&",
                    ),
                ),
                name="scheduling_z_buffer_practitioner_excl",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.UniqueConstraint(
                fields=("series", "series_index"),
                name="scheduling_appointment_series_index_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointment",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("series__isnull", True), ("series_index__isnull", True)),
                    models.Q(
                        ("series__isnull", False),
                        ("series_index__gte", 1),
                        ("series_index__lte", 52),
                    ),
                    _connector="OR",
                ),
                name="scheduling_appointment_series_shape",
            ),
        ),
        migrations.AddField(
            model_name="appointmenttransition",
            name="appointment",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="transitions",
                to="scheduling.appointment",
            ),
        ),
        migrations.AddField(
            model_name="appointmenttransition",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="appointmenttransition",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="seriesexception",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="seriesexception",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="seriesexception",
            name="series",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="exceptions",
                to="scheduling.appointmentseries",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentseries",
            constraint=models.UniqueConstraint(
                fields=("organization", "idempotency_key"),
                name="scheduling_series_org_idempotency_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentseries",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="scheduling_series_scope"
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentseries",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("frequency__in", ["weekly", "biweekly", "monthly"]),
                    ("weekday__lte", 6),
                    ("end_local__gt", models.F("start_local")),
                    models.Q(
                        models.Q(
                            ("frequency", "monthly"),
                            ("month_week__gte", 1),
                            ("month_week__lte", 4),
                        ),
                        models.Q(
                            models.Q(("frequency", "monthly"), _negated=True),
                            ("month_week__isnull", True),
                        ),
                        _connector="OR",
                    ),
                    models.Q(
                        models.Q(
                            ("count__gte", 1),
                            ("count__lte", 52),
                            ("until__isnull", True),
                        ),
                        models.Q(
                            ("count__isnull", True),
                            ("until__gte", models.F("first_date")),
                        ),
                        _connector="OR",
                    ),
                ),
                name="scheduling_series_rule",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttransition",
            constraint=models.UniqueConstraint(
                fields=("organization", "command_id"),
                name="scheduling_transition_command_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttransition",
            constraint=models.UniqueConstraint(
                fields=("appointment", "revision"),
                name="scheduling_transition_revision_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmenttransition",
            constraint=models.CheckConstraint(
                condition=models.Q(("actor_kind__in", ["staff", "patient", "machine"])),
                name="scheduling_transition_actor_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="seriesexception",
            constraint=models.UniqueConstraint(
                fields=("organization", "command_id"),
                name="scheduling_series_exception_command_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="seriesexception",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("scope__in", ["this", "future"]),
                    ("occurrence_index__gte", 1),
                    ("occurrence_index__lte", 52),
                    models.Q(
                        models.Q(("kind", "moved"), ("start_local__isnull", False)),
                        models.Q(("kind", "cancelled"), ("start_local__isnull", True)),
                        _connector="OR",
                    ),
                ),
                name="scheduling_series_exception_shape",
            ),
        ),
        *(
            migrations.RunSQL(
                apply_scheduling_rls(table, column),
                remove_scheduling_rls(table, column),
            )
            for table, column in sorted(LIFECYCLE_RLS_TARGETS)
        ),
        migrations.RunSQL(CONSTRAINT_SQL, REVERSE_CONSTRAINT_SQL),
        migrations.RunSQL(CLOCK_SQL, REVERSE_CLOCK_SQL),
        migrations.RunSQL(GUARD_SQL, REVERSE_GUARD_SQL),
        migrations.RunSQL(ACL_SQL, REVERSE_ACL_SQL),
        migrations.RunSQL(EHR_SQL, EHR_REVERSE_SQL),
    ]

"""Add resource definitions, concrete template blocks and capacity exclusions."""

import uuid
from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.scheduling.migrations import _resource_slots_sql
from apps.scheduling.migrations._resources_sql import (
    BINDING_SQL,
    DEFINITION_SQL,
    GUARDS_SQL,
    INTERVAL_SQL,
    REVERSE_INTERVAL_SQL,
    REVERSE_SQL,
)
from apps.scheduling.rls import (
    RESOURCE_RLS_TARGETS,
    apply_scheduling_rls,
    remove_scheduling_rls,
)


class Migration(migrations.Migration):
    """Install additive capacity guards without rewriting practitioner exclusions."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0013_permission_bundles"),
        ("intake", "0011_protected_fields"),
        ("scheduling", "0004_waitlistentry_waitlistoffer_and_more"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(INTERVAL_SQL, REVERSE_INTERVAL_SQL),
        migrations.CreateModel(
            name="ServiceType",
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
                ("name", models.CharField(max_length=120)),
                ("duration_min", models.PositiveSmallIntegerField()),
                ("buffer_before", models.PositiveSmallIntegerField(default=0)),
                ("buffer_after", models.PositiveSmallIntegerField(default=0)),
                (
                    "required_professional_roles",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=32),
                        default=list,
                        size=None,
                    ),
                ),
                (
                    "required_resource_kinds",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=16),
                        default=list,
                        size=None,
                    ),
                ),
                ("insurer_billable", models.BooleanField(default=False)),
                ("price_ref", models.CharField(blank=True, default="", max_length=80)),
                ("active", models.BooleanField(default=True)),
            ],
        ),
        migrations.AddField(
            model_name="appointment",
            name="buffer_after",
            field=models.PositiveSmallIntegerField(
                default=0, db_default=0, editable=False
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="buffer_before",
            field=models.PositiveSmallIntegerField(
                default=0, db_default=0, editable=False
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="resource_ids",
            field=django.contrib.postgres.fields.ArrayField(
                base_field=models.UUIDField(),
                blank=True,
                default=list,
                db_default=[],
                size=None,
            ),
        ),
        migrations.AddField(
            model_name="availabilityblock",
            name="generated_date",
            field=models.DateField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name="availabilityblock",
            name="practitioner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to=settings.AUTH_USER_MODEL,
            ),
        ),
        migrations.CreateModel(
            name="AvailabilityTemplate",
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
                    "weekdays",
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.PositiveSmallIntegerField(), size=None
                    ),
                ),
                ("start_local", models.TimeField()),
                ("end_local", models.TimeField()),
                ("valid_from", models.DateField()),
                ("valid_to", models.DateField()),
                ("timezone", models.CharField(max_length=63)),
                ("active", models.BooleanField(default=True)),
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
                    "practitioner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="availabilityblock",
            name="template",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="scheduling.availabilitytemplate",
            ),
        ),
        migrations.CreateModel(
            name="Holiday",
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
                (
                    "reason",
                    models.CharField(
                        choices=[("holiday", "Holiday"), ("closure", "Clinic closure")],
                        max_length=16,
                    ),
                ),
                ("active", models.BooleanField(default=True)),
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
        ),
        migrations.CreateModel(
            name="Resource",
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
                    "kind",
                    models.CharField(
                        choices=[
                            ("room", "Room"),
                            ("equipment", "Equipment"),
                            ("location", "Location"),
                        ],
                        max_length=16,
                    ),
                ),
                ("name", models.CharField(max_length=120)),
                ("capacity", models.PositiveSmallIntegerField(default=1)),
                ("active", models.BooleanField(default=True)),
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
        ),
        migrations.AddField(
            model_name="availabilitytemplate",
            name="resource",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="scheduling.resource",
            ),
        ),
        migrations.CreateModel(
            name="AppointmentResource",
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
                ("unit", models.PositiveSmallIntegerField()),
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("occupied", models.BooleanField(default=True)),
                (
                    "appointment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.appointment",
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
                (
                    "resource",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.resource",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="Absence",
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
                (
                    "reason",
                    models.CharField(
                        choices=[
                            ("unavailable", "Unavailable"),
                            ("maintenance", "Maintenance"),
                        ],
                        max_length=16,
                    ),
                ),
                ("active", models.BooleanField(default=True)),
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
                    "practitioner",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
                (
                    "resource",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.resource",
                    ),
                ),
            ],
        ),
        migrations.AddField(
            model_name="availabilityblock",
            name="resource",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="scheduling.resource",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityblock",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("practitioner__isnull", False), ("resource__isnull", True)
                    ),
                    models.Q(
                        ("practitioner__isnull", True), ("resource__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="scheduling_availability_subject",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityblock",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("generated_date__isnull", True), ("template__isnull", True)
                    ),
                    models.Q(
                        ("generated_date__isnull", False), ("template__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="scheduling_availability_generation",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityblock",
            constraint=models.UniqueConstraint(
                fields=("template", "generated_date"),
                name="scheduling_template_date_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilityblock",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("retired_at__isnull", True)),
                expressions=(
                    ("resource", "="),
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
                name="scheduling_availability_resource_excl",
            ),
        ),
        migrations.AddField(
            model_name="servicetype",
            name="clinic",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT, to="identity.clinic"
            ),
        ),
        migrations.AddField(
            model_name="servicetype",
            name="organization",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="identity.organization"
            ),
        ),
        migrations.AddField(
            model_name="appointment",
            name="service_type",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                to="scheduling.servicetype",
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
            model_name="holiday",
            constraint=models.CheckConstraint(
                condition=models.Q(("end_at__gt", models.F("start_at"))),
                name="scheduling_holiday_window",
            ),
        ),
        migrations.AddConstraint(
            model_name="holiday",
            constraint=models.CheckConstraint(
                condition=models.Q(("reason__in", ["holiday", "closure"])),
                name="scheduling_holiday_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="resource",
            constraint=models.CheckConstraint(
                condition=models.Q(("capacity__gte", 1), ("capacity__lte", 64)),
                name="scheduling_resource_capacity",
            ),
        ),
        migrations.AddConstraint(
            model_name="resource",
            constraint=models.CheckConstraint(
                condition=models.Q(("kind__in", ["room", "equipment", "location"])),
                name="scheduling_resource_kind",
            ),
        ),
        migrations.AddConstraint(
            model_name="resource",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_resource_scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilitytemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("practitioner__isnull", False), ("resource__isnull", True)
                    ),
                    models.Q(
                        ("practitioner__isnull", True), ("resource__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="scheduling_template_subject",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilitytemplate",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("end_local__gt", models.F("start_local")),
                    ("valid_to__gte", models.F("valid_from")),
                ),
                name="scheduling_template_window",
            ),
        ),
        migrations.AddConstraint(
            model_name="availabilitytemplate",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"),
                name="scheduling_template_scope",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentresource",
            constraint=models.UniqueConstraint(
                fields=("appointment", "resource"),
                name="scheduling_appointment_resource_uniq",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentresource",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("end_at__gt", models.F("start_at")),
                    ("unit__gte", 1),
                    ("unit__lte", 64),
                ),
                name="scheduling_reservation_window",
            ),
        ),
        migrations.AddConstraint(
            model_name="appointmentresource",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("occupied", True)),
                expressions=(
                    ("resource", "="),
                    ("unit", "="),
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
                name="scheduling_resource_capacity_excl",
            ),
        ),
        migrations.AddConstraint(
            model_name="absence",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("practitioner__isnull", False), ("resource__isnull", True)
                    ),
                    models.Q(
                        ("practitioner__isnull", True), ("resource__isnull", False)
                    ),
                    _connector="OR",
                ),
                name="scheduling_absence_subject",
            ),
        ),
        migrations.AddConstraint(
            model_name="absence",
            constraint=models.CheckConstraint(
                condition=models.Q(("end_at__gt", models.F("start_at"))),
                name="scheduling_absence_window",
            ),
        ),
        migrations.AddConstraint(
            model_name="absence",
            constraint=models.CheckConstraint(
                condition=models.Q(("reason__in", ["unavailable", "maintenance"])),
                name="scheduling_absence_reason",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicetype",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("buffer_after__lte", 240),
                    ("buffer_before__lte", 240),
                    ("duration_min__gte", 1),
                    ("duration_min__lte", 720),
                ),
                name="scheduling_service_duration",
            ),
        ),
        migrations.AddConstraint(
            model_name="servicetype",
            constraint=models.UniqueConstraint(
                fields=("organization", "clinic", "id"), name="scheduling_service_scope"
            ),
        ),
        *(
            migrations.RunSQL(
                apply_scheduling_rls(table, column),
                remove_scheduling_rls(table, column),
            )
            for table, column in sorted(RESOURCE_RLS_TARGETS)
        ),
        migrations.RunSQL(GUARDS_SQL + DEFINITION_SQL + BINDING_SQL, REVERSE_SQL),
        migrations.RunSQL(_resource_slots_sql.SQL, _resource_slots_sql.REVERSE_SQL),
    ]

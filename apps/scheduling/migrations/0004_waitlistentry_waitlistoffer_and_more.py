"""Persist FIFO requests and non-reserving expiring offers under scoped RLS."""

import uuid
from typing import ClassVar

import django.contrib.postgres.constraints
import django.contrib.postgres.fields.ranges
import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from ._waitlist_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Create the waitlist schema and its closed authority boundary."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("identity", "0005_clinic_timezone"),
        (
            "intake",
            "0006_remove_patientchannelpreference_intake_preference_purpose_check_and_more",
        ),
        ("scheduling", "0003_patient_booking"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="WaitlistEntry",
            fields=[
                ("id", models.BigAutoField(primary_key=True, serialize=False)),
                ("practitioner_label", models.CharField(max_length=150)),
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("waiting", "Na fila"),
                            ("offered", "Oferta enviada ao portal"),
                            ("expired", "Oferta expirada"),
                            ("declined", "Oferta recusada"),
                            ("fulfilled", "Consulta agendada"),
                        ],
                        default="waiting",
                        max_length=16,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
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
                    "enrollment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="intake.patientclinicenrollment",
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
                        on_delete=django.db.models.deletion.PROTECT, to="intake.patient"
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
        ),
        migrations.CreateModel(
            name="WaitlistOffer",
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
                ("expires_at", models.DateTimeField()),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("pending", "Aguardando resposta"),
                            ("accepted", "Consulta agendada"),
                            ("expired", "Expirada"),
                            ("declined", "Recusada"),
                            ("unavailable", "Horário indisponível"),
                        ],
                        default="pending",
                        max_length=16,
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                ("responded_at", models.DateTimeField(blank=True, null=True)),
                (
                    "appointment",
                    models.OneToOneField(
                        blank=True,
                        null=True,
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
                    "entry",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="offers",
                        to="scheduling.waitlistentry",
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
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
        ),
        migrations.AddIndex(
            model_name="waitlistentry",
            index=models.Index(
                fields=["clinic", "practitioner", "state", "id"],
                name="waitlist_fifo_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistentry",
            constraint=models.CheckConstraint(
                condition=models.Q(("end_at__gt", models.F("start_at"))),
                name="waitlist_entry_window",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistentry",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        ["waiting", "offered", "expired", "declined", "fulfilled"],
                    )
                ),
                name="waitlist_entry_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistoffer",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("end_at__gt", models.F("start_at")),
                    ("expires_at__gt", models.F("created_at")),
                ),
                name="waitlist_offer_window",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistoffer",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "state__in",
                        ["pending", "accepted", "expired", "declined", "unavailable"],
                    )
                ),
                name="waitlist_offer_state",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistoffer",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("appointment__isnull", False), ("state", "accepted")),
                    models.Q(
                        models.Q(("state", "accepted"), _negated=True),
                        ("appointment__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="waitlist_offer_booking",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistoffer",
            constraint=models.UniqueConstraint(
                condition=models.Q(("state", "pending")),
                fields=("entry",),
                name="waitlist_entry_one_offer",
            ),
        ),
        migrations.AddConstraint(
            model_name="waitlistoffer",
            constraint=django.contrib.postgres.constraints.ExclusionConstraint(
                condition=models.Q(("state", "pending")),
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
                name="waitlist_offer_one_opening",
            ),
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]

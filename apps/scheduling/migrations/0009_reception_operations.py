"""Add retained reception queues, room conditions, delays and display devices."""

import uuid
from typing import ClassVar

import django.db.models.deletion
import django.utils.timezone
from django.conf import settings
from django.db import migrations, models

from ._reception_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install narrowed runtime grants and guards with the additive schema."""

    dependencies: ClassVar = [
        ("identity", "0038_reception_permission"),
        ("scheduling", "0008_expiry_audit"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar = [
        migrations.CreateModel(
            name="DisplayDevice",
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
                    "token_hash",
                    models.CharField(editable=False, max_length=64, unique=True),
                ),
                ("active", models.BooleanField(default=True)),
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
            ],
            options={"abstract": False},
        ),
        migrations.CreateModel(
            name="ClinicianDelay",
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
                ("minutes", models.PositiveSmallIntegerField(default=0)),
                ("revision", models.PositiveIntegerField(default=1, editable=False)),
                (
                    "updated_at",
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
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "practitioner"),
                        name="scheduling_delay_practitioner",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(minutes__lte=720),
                        name="scheduling_delay_minutes",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="QueueEntry",
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
                ("position", models.PositiveBigIntegerField(default=0, editable=False)),
                (
                    "ticket_code",
                    models.CharField(default="", editable=False, max_length=32),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("waiting", "Waiting"),
                            ("called", "Called"),
                            ("in_room", "In room"),
                            ("done", "Done"),
                        ],
                        default="waiting",
                        max_length=16,
                    ),
                ),
                ("room_unit", models.PositiveSmallIntegerField(null=True)),
                ("called_at", models.DateTimeField(null=True)),
                ("revision", models.PositiveIntegerField(default=1, editable=False)),
                (
                    "created_at",
                    models.DateTimeField(
                        default=django.utils.timezone.now, editable=False
                    ),
                ),
                (
                    "appointment",
                    models.OneToOneField(
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
                    "location",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="scheduling.resource",
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
                    "room",
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="+",
                        to="scheduling.resource",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("clinic", "position"),
                        name="scheduling_queue_position",
                    ),
                    models.UniqueConstraint(
                        fields=("clinic", "ticket_code"), name="scheduling_queue_ticket"
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(state__in=("called", "in_room")),
                        fields=("room", "room_unit"),
                        name="scheduling_queue_room_occupied",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            state__in=("waiting", "called", "in_room", "done")
                        ),
                        name="scheduling_queue_state",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(
                                called_at__isnull=True,
                                room__isnull=True,
                                room_unit__isnull=True,
                            )
                            | models.Q(
                                called_at__isnull=False,
                                room__isnull=False,
                                room_unit__gte=1,
                                room_unit__lte=64,
                            )
                        ),
                        name="scheduling_queue_room_binding",
                    ),
                ]
            },
        ),
        migrations.CreateModel(
            name="RoomStatus",
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
                ("unit", models.PositiveSmallIntegerField(default=1)),
                (
                    "display_number",
                    models.PositiveIntegerField(default=0, editable=False),
                ),
                (
                    "state",
                    models.CharField(
                        choices=[
                            ("available", "Available"),
                            ("cleaning", "Cleaning"),
                            ("unavailable", "Unavailable"),
                        ],
                        default="available",
                        max_length=16,
                    ),
                ),
                ("revision", models.PositiveIntegerField(default=1, editable=False)),
                (
                    "updated_at",
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
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
                    ),
                ),
                (
                    "room",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.resource",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("room", "unit"), name="scheduling_room_status_unit"
                    ),
                    models.UniqueConstraint(
                        fields=("clinic", "display_number"),
                        name="scheduling_room_display_number",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(unit__gte=1, unit__lte=64),
                        name="scheduling_room_status_capacity",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(
                            state__in=("available", "cleaning", "unavailable")
                        ),
                        name="scheduling_room_status_state",
                    ),
                ]
            },
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]

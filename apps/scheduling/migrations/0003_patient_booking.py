"""Add explicit patient scheduling authority without staff impersonation."""

import uuid
from typing import ClassVar

import django.db.models.deletion
from django.db import migrations, models
from django.db.migrations.operations.base import Operation

from apps.scheduling.migrations._appointment_sql import INTEGRITY_SQL
from apps.scheduling.migrations._patient_booking_sql import REVERSE_SQL, SQL

ORIGINAL_GUARD = (
    "CREATE OR REPLACE FUNCTION clinic_app.scheduling_appointment_guard_v1()"
    + INTEGRITY_SQL.split(
        "CREATE FUNCTION clinic_app.scheduling_appointment_guard_v1()", 1
    )[1].split("REVOKE ALL", 1)[0]
)
PATIENT_GUARD = ORIGINAL_GUARD.replace(
    "        SELECT clinic.timezone",
    "        IF NULLIF(current_setting('app.current_patient_session', true), '') "
    "IS NOT NULL THEN\n"
    "            SELECT s.timezone INTO clinic_timezone\n"
    "              FROM clinic_app.patient_booking_scope() s\n"
    "             WHERE s.clinic_id = NEW.clinic_id\n"
    "               AND s.organization_id = NEW.organization_id;\n"
    "        ELSE\n"
    "        SELECT clinic.timezone",
).replace(
    "AND clinic.id = NEW.clinic_id;",
    "AND clinic.id = NEW.clinic_id;\n        END IF;",
)


class Migration(migrations.Migration):
    """Install narrow RLS, session-bound projections and immutable receipts."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("scheduling", "0002_appointment"),
        ("intake", "0005_booking_operation"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.CreateModel(
            name="PatientBookingEvent",
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
                ("patient_session_id", models.UUIDField()),
                ("action", models.CharField(max_length=16)),
                ("start_at", models.DateTimeField()),
                ("end_at", models.DateTimeField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "organization",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to="identity.organization",
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
                    "appointment",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to="scheduling.appointment",
                    ),
                ),
            ],
        ),
        migrations.RunSQL(SQL, REVERSE_SQL),
        migrations.RunSQL(PATIENT_GUARD, ORIGINAL_GUARD),
    ]

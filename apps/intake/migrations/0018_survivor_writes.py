"""Reject new bookings, encounters and charges on an active alias."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._survivor_write_sql import SQL


class Migration(migrations.Migration):
    """Add insert guards while retaining every original immutable binding."""

    dependencies: ClassVar = [
        ("intake", "0017_patient_aliases"),
        ("scheduling", "0006_capacity_insert_authorization"),
        ("ehr", "0009_protected_fields"),
        ("billing", "0009_protected_fields"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]

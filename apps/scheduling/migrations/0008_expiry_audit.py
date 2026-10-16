"""Audit machine hold expiry and gate it like every appointment write."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.scheduling.migrations._expiry_audit_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Refuse a populated reverse: machine ledger rows need a restore."""

    dependencies: ClassVar[list[tuple[str, str]]] = [
        ("audit", "0005_clinic_metadata_v2"),
        ("scheduling", "0007_appointment_lifecycle_v2"),
    ]
    operations: ClassVar[list[Operation]] = [
        migrations.RunSQL(SQL, REVERSE_SQL),
    ]

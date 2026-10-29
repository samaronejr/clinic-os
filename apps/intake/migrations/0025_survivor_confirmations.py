"""Reject fresh hold/booking confirmations on aliases, preserving lawful exits."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._survivor_confirmation_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Extend the existing guard without changing any role, grant or retained row."""

    dependencies: ClassVar = [
        ("intake", "0024_document_decrypt_context"),
        ("scheduling", "0008_expiry_audit"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]

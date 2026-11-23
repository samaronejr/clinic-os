"""Restore patient-session scope after opening retained prescription content."""

from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.intake.migrations._document_decrypt_context_sql import SQL


class Migration(migrations.Migration):
    """Keep alias read and audit authority unchanged after successful decryption."""

    dependencies: ClassVar = [
        ("intake", "0023_duplicate_lookup_indexes"),
        ("prescription", "0010_protected_documents"),
    ]
    operations: ClassVar[list[Operation]] = [migrations.RunSQL(SQL)]

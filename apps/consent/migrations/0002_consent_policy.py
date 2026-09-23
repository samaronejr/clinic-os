"""Enforce immutable consent and independent patient authority in PostgreSQL."""

from collections.abc import Sequence
from typing import ClassVar

from django.db import migrations
from django.db.migrations.operations.base import Operation

from ._consent_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Install consent policy only after explicit operation support exists."""

    dependencies: ClassVar[Sequence[tuple[str, str]]] = [
        ("consent", "0001_initial"),
        (
            "intake",
            "0008_remove_patientaccessgrant_intake_grant_operations_check_and_more",
        ),
        ("intake", "0004_questionnaires"),
    ]
    operations: ClassVar[Sequence[Operation]] = [migrations.RunSQL(SQL, REVERSE_SQL)]

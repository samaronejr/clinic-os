"""Enforce clinic authority, retained revisions and immutable settlement history."""

from typing import ClassVar

from django.db import migrations

from ._invoice_sql import REVERSE_SQL, SQL


class Migration(migrations.Migration):
    """Keep runtime access distinct from database-owned history writes."""

    dependencies: ClassVar = [
        ("billing", "0001_initial"),
        ("ehr", "0008_finalization_policy"),
        (
            "intake",
            "0010_remove_patientaccessgrant_intake_grant_operations_check_and_more",
        ),
    ]
    operations: ClassVar = [migrations.RunSQL(SQL, REVERSE_SQL)]

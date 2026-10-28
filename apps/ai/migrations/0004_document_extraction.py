"""Add protected extraction drafts; irreversible, rollback = restore."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _extraction_review_sql, _extraction_sql
from apps.ai.migrations._extraction_tables import OPERATIONS


class ProtectedSchema(migrations.SeparateDatabaseAndState):
    """Publish ciphertext tables and the complete least-privilege contract together."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """Never weaken existing source immutability or identity matching."""

    atomic = False
    dependencies: ClassVar = [
        ("ai", "0003_artifact_retention"),
        ("orders", "0004_matched_patient_authority"),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[
                *OPERATIONS,
                migrations.RunSQL(_extraction_sql.SQL + _extraction_review_sql.SQL),
            ],
            state_operations=OPERATIONS,
        ),
    ]

"""Add evaluation gates and protected data; irreversible, rollback = restore."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _evaluation_sql
from apps.ai.migrations._evaluation_tables import OPERATIONS
from apps.tenancy.migration_operations import SeparateDatabaseAndState


class ProtectedSchema(SeparateDatabaseAndState):
    """Commit encrypted observations and their independent SQL guards together."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """No external approval or real-provider route is installed."""

    atomic = False
    dependencies: ClassVar = [
        ("ai", "0004_document_extraction"),
        ("identity", "0041_retention_authority"),
        ("prescription", "0015_sncr_issuance"),
        ("retention", "0005_transient_purge"),
        ("scheduling", "0009_reception_operations"),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[*OPERATIONS, migrations.RunSQL(_evaluation_sql.SQL)],
            state_operations=OPERATIONS,
        )
    ]

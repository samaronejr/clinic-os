"""Add evaluation gates and protected data; irreversible, rollback = restore."""

from typing import ClassVar

from django.db import migrations

from apps.ai.migrations import _evaluation_sql
from apps.ai.migrations._evaluation_tables import CONFIGURATION_FIELDS, TABLE_OPERATIONS
from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.tenancy.migrations._resumable_schema import GuardedMigration


class ProtectedSchema(SchemaStateBatch):
    """Commit encrypted observations and their independent SQL guards together."""

    atomic = True
    reversible = False


class Migration(GuardedMigration):
    """No external approval or real-provider route is installed."""

    dependencies: ClassVar = [
        ("ai", "0004_document_extraction"),
        ("identity", "0041_retention_authority"),
        ("prescription", "0015_sncr_issuance"),
        ("retention", "0005_transient_purge"),
        ("scheduling", "0009_reception_operations"),
    ]
    operations: ClassVar = [
        *CONFIGURATION_FIELDS,
        ProtectedSchema(
            database_operations=[
                *TABLE_OPERATIONS,
                migrations.RunSQL(_evaluation_sql.SQL),
            ],
            state_operations=TABLE_OPERATIONS,
        ),
    ]

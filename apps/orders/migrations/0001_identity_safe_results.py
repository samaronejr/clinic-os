"""Encrypted result sources are irreversible; rollback = restore."""

from typing import ClassVar

from django.conf import settings
from django.db import migrations
from django.db.migrations.operations.base import Operation

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.orders.migrations._contract_sql import CONTRACT
from apps.orders.migrations._schema import MODELS
from apps.tenancy.migrations._resumable_schema import GuardedMigration


class ProtectedSchema(SchemaStateBatch):
    """Publish the encrypted schema and its least-privilege contract together."""

    atomic = True
    reversible = False


class Migration(GuardedMigration):
    """No plaintext conversion, and no reverse path that can erase evidence."""

    dependencies: ClassVar = [
        ("identity", "0038_reception_permission"),
        ("intake", "0025_survivor_confirmations"),
        ("ehr", "0014_longitudinal_record"),
        ("workflows", "0004_task_completion_gate"),
        ("audit", "0005_clinic_metadata_v2"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar[list[Operation]] = [
        ProtectedSchema(
            database_operations=[*MODELS, migrations.RunSQL(CONTRACT)],
            state_operations=MODELS,
        ),
    ]

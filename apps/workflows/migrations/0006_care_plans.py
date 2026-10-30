"""Add care plans, referrals, recalls and PRO assignments; rollback = restore.

The tables carry encrypted clinical text from their first version, so this
follows the protected-schema convention: irreversible, forward corrections only.
"""

from typing import ClassVar

from django.conf import settings
from django.db import migrations

from apps.ai.migrations._state_batch import SchemaStateBatch
from apps.workflows.migrations._care_sql import SQL
from apps.workflows.migrations._care_tables import OPERATIONS


class ProtectedSchema(SchemaStateBatch):
    """Publish ciphertext tables and their least-privilege contract together."""

    atomic = True
    reversible = False


class Migration(migrations.Migration):
    """Additive only; no active run, task or clinical record is rewritten."""

    atomic = False
    dependencies: ClassVar = [
        ("ehr", "0016_later_pack_draft_guard"),
        ("identity", "0041_retention_authority"),
        ("intake", "0025_survivor_confirmations"),
        ("prescription", "0015_sncr_issuance"),
        # The care guard replaces the deployed task guard (retention's text) to
        # admit the PRO review task; it must follow that definition.
        ("retention", "0005_transient_purge"),
        ("workflows", "0005_result_source_tasks"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]
    operations: ClassVar = [
        ProtectedSchema(
            database_operations=[*OPERATIONS, migrations.RunSQL(SQL)],
            state_operations=OPERATIONS,
        ),
    ]
